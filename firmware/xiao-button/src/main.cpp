// A battery powered button that starts a Mira Mode preset.
//
// The board sleeps until the button pulls its pin low, then wakes,
// connects to the valve, writes one seven byte frame and sleeps again.
// Starting a preset is a constant frame, so there is no protocol to
// speak beyond writing those bytes and waiting to be acknowledged.
//
// Deep sleep is what makes this last on a battery, so the whole job has
// to finish and get back to sleep quickly. The valve's address is kept
// in RTC memory, which survives deep sleep, so only the first press
// after a power cycle pays for a scan.

#include <Arduino.h>
#include <NimBLEDevice.h>

#include "driver/gpio.h"
#include "esp_sleep.h"

// ---------------------------------------------------------------- config

// Which preset the button starts. Read them off the valve with
// `miramode presets -a <address>`; on the test unit slot 1 is the
// shower and slot 2 the bath fill.
static constexpr uint8_t PRESET_SLOT = 2;

// Button to ground on this pin. Only GPIO 0-5 can wake an ESP32-C3 from
// deep sleep, so it has to be one of those.
static constexpr gpio_num_t BUTTON_PIN = GPIO_NUM_2;

// Optional LED to ground through a resistor, for feedback. The XIAO
// ESP32C3 has no user LED of its own, so set this to GPIO_NUM_NC to go
// without one.
static constexpr gpio_num_t LED_PIN = GPIO_NUM_3;

// How long to look for the valve when its address isn't already known.
static constexpr uint32_t SCAN_SECONDS = 5;
// How long to wait for the valve to acknowledge the command.
static constexpr uint32_t ACK_TIMEOUT_MS = 3000;

static const NimBLEUUID SERVICE_UUID("267f0001-eb15-43f5-94c3-67d2221188f7");
static const NimBLEUUID COMMAND_UUID("267f0002-eb15-43f5-94c3-67d2221188f7");
static const NimBLEUUID EVENT_UUID("267f0003-eb15-43f5-94c3-67d2221188f7");

// ----------------------------------------------------------------- state

// RTC memory survives deep sleep but not a power cycle, which is the
// behaviour wanted: cache the valve between presses, rediscover it if
// the battery is changed or it is replaced.
RTC_DATA_ATTR static uint8_t cachedAddress[6];
RTC_DATA_ATTR static uint8_t cachedAddressType = 0;
RTC_DATA_ATTR static bool addressIsCached = false;

static volatile bool acknowledged = false;

// ------------------------------------------------------------- protocol

// Two's complement of every preceding byte, so a whole valid frame sums
// to zero modulo 256.
static uint8_t checksum(const uint8_t *data, size_t length) {
  uint8_t total = 0;
  for (size_t i = 0; i < length; i++) {
    total += data[i];
  }
  return static_cast<uint8_t>(-total);
}

// aa 55 <channel> <opcode> <length> <payload> <checksum>
static void buildRunPreset(uint8_t slot, uint8_t *out) {
  out[0] = 0xAA;  // preamble
  out[1] = 0x55;
  out[2] = 0x00;  // channel
  out[3] = 0xB1;  // run preset
  out[4] = 0x01;  // payload length
  out[5] = slot;
  out[6] = checksum(out, 6);
}

// A reply is the same shape. Anything carrying a payload of 0x01 is the
// valve saying it accepted the command.
static bool isSuccess(const uint8_t *data, size_t length) {
  if (length < 6 || data[0] != 0xAA || data[1] != 0x55) {
    return false;
  }
  if (length != static_cast<size_t>(data[4]) + 6) {
    return false;
  }
  uint8_t total = 0;
  for (size_t i = 0; i < length; i++) {
    total += data[i];
  }
  return total == 0 && data[4] >= 1 && data[5] == 0x01;
}

// ------------------------------------------------------------ callbacks

class ClientCallbacks : public NimBLEClientCallbacks {
  void onDisconnect(NimBLEClient *client) override {
    Serial.println("disconnected");
  }

  // The valve pairs without a passkey, so accept Just Works and let
  // NimBLE store the bond in NVS for next time.
  uint32_t onPassKeyRequest() override { return 0; }
  bool onConfirmPIN(uint32_t) override { return true; }

  void onAuthenticationComplete(ble_gap_conn_desc *desc) override {
    Serial.printf("bonded=%d encrypted=%d\n", desc->sec_state.bonded,
                  desc->sec_state.encrypted);
  }
};

static ClientCallbacks clientCallbacks;

static void onNotify(NimBLERemoteCharacteristic *, uint8_t *data, size_t length,
                     bool) {
  if (isSuccess(data, length)) {
    acknowledged = true;
  }
}

// ------------------------------------------------------------- feedback

static void blink(int times, int onMs) {
  if (LED_PIN == GPIO_NUM_NC) {
    return;
  }
  for (int i = 0; i < times; i++) {
    digitalWrite(LED_PIN, HIGH);
    delay(onMs);
    digitalWrite(LED_PIN, LOW);
    delay(120);
  }
}

// Wait for the button to come back up, so releasing it doesn't wake the
// board straight back up, then sleep until it is pressed again.
static void sleepUntilPressed() {
  Serial.println("sleeping");
  Serial.flush();

  while (digitalRead(BUTTON_PIN) == LOW) {
    delay(10);
  }
  delay(50);  // let the contact settle

  if (LED_PIN != GPIO_NUM_NC) {
    digitalWrite(LED_PIN, LOW);
    gpio_hold_dis(LED_PIN);
  }

  gpio_pullup_en(BUTTON_PIN);
  gpio_pulldown_dis(BUTTON_PIN);
  esp_deep_sleep_enable_gpio_wakeup(1ULL << BUTTON_PIN,
                                    ESP_GPIO_WAKEUP_GPIO_LOW);
  esp_deep_sleep_start();  // never returns
}

// ---------------------------------------------------------------- valve

// Look for anything advertising the valve's service.
static bool findValve() {
  Serial.println("scanning");
  NimBLEScan *scan = NimBLEDevice::getScan();
  scan->setActiveScan(true);
  scan->setInterval(45);
  scan->setWindow(15);

  NimBLEScanResults found = scan->start(SCAN_SECONDS, false);
  for (int i = 0; i < found.getCount(); i++) {
    NimBLEAdvertisedDevice device = found.getDevice(i);
    if (!device.isAdvertisingService(SERVICE_UUID)) {
      continue;
    }
    NimBLEAddress address = device.getAddress();
    memcpy(cachedAddress, address.getNative(), sizeof(cachedAddress));
    cachedAddressType = address.getType();
    addressIsCached = true;
    Serial.printf("found %s\n", address.toString().c_str());
    scan->clearResults();
    return true;
  }
  Serial.println("no valve advertising");
  scan->clearResults();
  return false;
}

// Connect, write the command, and wait to be told it was accepted.
static bool runPreset() {
  NimBLEClient *client = NimBLEDevice::createClient();
  client->setClientCallbacks(&clientCallbacks, false);
  client->setConnectTimeout(10);

  NimBLEAddress address(cachedAddress, cachedAddressType);
  if (!client->connect(address)) {
    Serial.println("connect failed");
    NimBLEDevice::deleteClient(client);
    return false;
  }

  // The valve ignores commands from an unbonded peer, so this has to
  // succeed before anything is written. After the first time the keys
  // come from NVS and it is quick.
  if (!client->secureConnection()) {
    Serial.println("pairing failed");
    client->disconnect();
    NimBLEDevice::deleteClient(client);
    return false;
  }

  NimBLERemoteService *service = client->getService(SERVICE_UUID);
  if (service == nullptr) {
    Serial.println("service missing - is this a GCS valve?");
    client->disconnect();
    NimBLEDevice::deleteClient(client);
    return false;
  }

  NimBLERemoteCharacteristic *events = service->getCharacteristic(EVENT_UUID);
  if (events != nullptr && events->canNotify()) {
    events->subscribe(true, onNotify);
  }

  NimBLERemoteCharacteristic *command =
      service->getCharacteristic(COMMAND_UUID);
  if (command == nullptr) {
    Serial.println("command characteristic missing");
    client->disconnect();
    NimBLEDevice::deleteClient(client);
    return false;
  }

  uint8_t frame[7];
  buildRunPreset(PRESET_SLOT, frame);
  Serial.printf("writing preset %u\n", PRESET_SLOT);
  acknowledged = false;
  // Written without a response, as the phone app does.
  command->writeValue(frame, sizeof(frame), false);

  uint32_t deadline = millis() + ACK_TIMEOUT_MS;
  while (!acknowledged && millis() < deadline) {
    delay(10);
  }

  client->disconnect();
  NimBLEDevice::deleteClient(client);

  if (!acknowledged) {
    Serial.println("no acknowledgement");
    return false;
  }
  Serial.println("started");
  return true;
}

// ----------------------------------------------------------------- main

void setup() {
  Serial.begin(115200);

  pinMode(BUTTON_PIN, INPUT_PULLUP);
  if (LED_PIN != GPIO_NUM_NC) {
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LOW);
  }

  // A cold boot is usually someone plugging it in rather than pressing
  // the button, so just go to sleep and wait to be asked.
  if (esp_sleep_get_wakeup_cause() != ESP_SLEEP_WAKEUP_GPIO) {
    Serial.println("powered on");
    delay(200);  // long enough to catch the serial monitor
    sleepUntilPressed();
  }

  blink(1, 40);  // acknowledge the press immediately

  NimBLEDevice::init("mira-button");
  NimBLEDevice::setPower(ESP_PWR_LVL_P9);
  // Bond, no man-in-the-middle protection, secure connections. The
  // board has no display or keyboard, so pairing is Just Works.
  NimBLEDevice::setSecurityAuth(true, false, true);
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);

  bool started = false;
  if (addressIsCached) {
    started = runPreset();
  }
  // Either the address was never known, or the valve has moved or been
  // replaced since it was cached.
  if (!started && findValve()) {
    started = runPreset();
  }

  blink(started ? 2 : 5, started ? 120 : 60);
  sleepUntilPressed();
}

void loop() {
  // Never reached: setup() always ends in deep sleep.
}
