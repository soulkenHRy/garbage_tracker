/**********************************************************************
  Smart Bin board: Freenove ESP32-S3 WROOM + camera + HC-SR04 hand sensor.
  No WiFi and no web server: the board talks to the Smart Bin app (app.py)
  over the USB cable, so keep it plugged into the laptop.

  Arduino IDE, Tools menu:  Board "ESP32S3 Dev Module",
                            USB CDC On Boot "Enabled",  PSRAM "OPI PSRAM".
  Use the same USB socket on the board as for sensor_test.ino.

  The board sends text lines, and after a PHOTO line the photo itself:
    D 57.2 30             distance in cm, 5 times a second ("-" = nothing in range),
                          then the photo distance it is using
    PHOTO hand 11873 210  something came closer than the photo distance. The 11873
                          bytes that follow are the JPEG; taking it took 210 ms
    PHOTO scan 11873 210  the same, in answer to a "scan" command
    ERR hand  /  ERR scan the camera could not take that photo
  The board understands (one command per line):
    scan                  take a photo now and send it (the app's Scan button)
    near 30               change the photo distance, in cm (0 = no automatic photos)
**********************************************************************/
#include "esp_camera.h"

#if !ARDUINO_USB_CDC_ON_BOOT
#error "In the Arduino IDE set Tools > USB CDC On Boot to Enabled, then upload again."
#endif

// Hand sensor (HC-SR04). Wiring: Vcc -> 3V3, Gnd -> GND, Trig -> GPIO 21, Echo -> GPIO 47
#define TRIG_PIN 21
#define ECHO_PIN 47
int nearCm = 30;  // take a photo when something comes this close (cm). The app can change it.

// Camera pins on the Freenove ESP32-S3 WROOM board ("ESP32S3_EYE" in the camera examples)
#define XCLK_GPIO_NUM 15
#define SIOD_GPIO_NUM 4
#define SIOC_GPIO_NUM 5
#define Y2_GPIO_NUM 11
#define Y3_GPIO_NUM 9
#define Y4_GPIO_NUM 8
#define Y5_GPIO_NUM 10
#define Y6_GPIO_NUM 12
#define Y7_GPIO_NUM 18
#define Y8_GPIO_NUM 17
#define Y9_GPIO_NUM 16
#define VSYNC_GPIO_NUM 6
#define HREF_GPIO_NUM 7
#define PCLK_GPIO_NUM 13

bool cameraOk = false;

// Same camera settings as the CameraWebServer sketch used: 320x240, same brightness and orientation.
bool startCamera() {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = -1;
  config.pin_reset = -1;
  config.xclk_freq_hz = 10000000;
  config.frame_size = FRAMESIZE_QVGA;
  config.pixel_format = PIXFORMAT_JPEG;  // for cameras that make JPEGs themselves
  config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  config.fb_location = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
  config.jpeg_quality = 10;
  config.fb_count = 1;

  esp_err_t err = esp_camera_init(&config);
  if (err == ESP_ERR_NOT_SUPPORTED) {  // this board's camera only gives raw pixels; takePhoto() makes the JPEG
    config.pixel_format = PIXFORMAT_RGB565;
    err = esp_camera_init(&config);
  }
  if (err != ESP_OK) return false;

  sensor_t *s = esp_camera_sensor_get();
  uint16_t pid = s->id.PID;
  if (pid == OV2640_PID) {
    s->set_hmirror(s, 1);
    s->set_vflip(s, 1);
  } else if (pid == GC2145_PID || pid == GC0308_PID) {
    s->set_hmirror(s, 0);
    delay(500);
    s->set_vflip(s, 0);
  } else {
    s->set_hmirror(s, 1);
    s->set_vflip(s, 0);
  }
  s->set_brightness(s, 1);
  s->set_saturation(s, 0);
  s->set_ae_level(s, -3);
  return true;
}

// Takes a photo right now and hands it over as a JPEG. The caller frees it with free().
bool takePhoto(uint8_t **jpg, size_t *len) {
  if (!cameraOk) return false;
  camera_fb_t *fb = esp_camera_fb_get();  // the camera keeps one picture waiting, taken a while ago: drop it
  if (fb) esp_camera_fb_return(fb);
  fb = esp_camera_fb_get();               // this picture is taken now
  if (!fb) return false;
  bool ok;
  if (fb->format == PIXFORMAT_JPEG) {
    *jpg = (uint8_t *)malloc(fb->len);
    *len = fb->len;
    ok = *jpg != NULL;
    if (ok) memcpy(*jpg, fb->buf, fb->len);
  } else {
    ok = frame2jpg(fb, 80, jpg, len);
  }
  esp_camera_fb_return(fb);
  return ok;
}

// Sends bytes over USB in pieces that fit the USB buffer, so none get dropped.
// Gives up (returns false) if the laptop stops reading for a second.
bool sendAll(const uint8_t *data, size_t len) {
  uint32_t lastProgress = millis();
  while (len > 0) {
    size_t room = Serial.availableForWrite();
    if (room > 0) {
      size_t n = room < len ? room : len;
      Serial.write(data, n);
      data += n;
      len -= n;
      lastProgress = millis();
    } else if (millis() - lastProgress > 1000) {
      return false;
    } else {
      delay(1);
    }
  }
  return true;
}

// Takes a photo and sends it: a "PHOTO ..." line with the size, then the JPEG bytes.
void sendPhoto(const char *why) {
  uint32_t started = millis();
  uint8_t *jpg = NULL;
  size_t len = 0;
  char line[48];
  if (!takePhoto(&jpg, &len)) {
    snprintf(line, sizeof(line), "\nERR %s\n", why);
    sendAll((const uint8_t *)line, strlen(line));
    return;
  }
  snprintf(line, sizeof(line), "\nPHOTO %s %u %lu\n", why, (unsigned)len, (unsigned long)(millis() - started));
  if (sendAll((const uint8_t *)line, strlen(line)) && sendAll(jpg, len)) sendAll((const uint8_t *)"\n", 1);
  free(jpg);
}

// One HC-SR04 measurement in cm, or -1 if no echo came back (same method as sensor_test.ino)
float measureCm() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  long duration = pulseIn(ECHO_PIN, HIGH, 30000);
  return duration == 0 ? -1 : duration * 0.0343 / 2.0;
}

void setup() {
  Serial.setTxBufferSize(2048);  // USB buffer big enough for a photo to leave in a few large pieces
  Serial.begin(115200);
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  digitalWrite(TRIG_PIN, LOW);
  cameraOk = startCamera();
}

void loop() {
  // Read the sensor about 15 times a second and keep the middle of the last three readings,
  // so one odd echo cannot set off a photo.
  static float recent[3] = {9999, 9999, 9999};
  static int next = 0;
  float cm = measureCm();
  recent[next] = cm < 0 ? 9999 : cm;  // "no echo" counts as very far away
  next = (next + 1) % 3;
  float a = recent[0], b = recent[1], c = recent[2];
  float middle = max(min(a, b), min(max(a, b), c));

  // Something has come close: take one photo and send it. No second photo until it has moved away
  // again (5 cm past the photo distance, so a hand hovering right at the limit cannot fire twice).
  static bool armed = true;
  if (middle > nearCm + 5) armed = true;
  if (armed && nearCm > 0 && middle <= nearCm) {
    armed = false;
    if (Serial.availableForWrite() > 1024) sendPhoto("hand");  // a full buffer means the app is not running
  }

  // The distance, 5 times a second (skipped while nobody is reading, so the board never waits)
  static uint32_t lastLine = 0;
  if (millis() - lastLine >= 200 && Serial.availableForWrite() >= 32) {
    if (middle > 9000) Serial.printf("D - %d\n", nearCm);
    else Serial.printf("D %.1f %d\n", middle, nearCm);
    lastLine = millis();
  }

  // Commands from the app, one per line
  static char command[24];
  static size_t commandLen = 0;
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      command[commandLen] = 0;
      commandLen = 0;
      if (strcmp(command, "scan") == 0) sendPhoto("scan");
      else if (strncmp(command, "near ", 5) == 0) nearCm = constrain(atoi(command + 5), 0, 400);
    } else if (commandLen < sizeof(command) - 1) {
      command[commandLen++] = ch;
    }
  }
  delay(60);
}
