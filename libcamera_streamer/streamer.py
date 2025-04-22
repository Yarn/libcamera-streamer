#!/usr/bin/env python3

# This was supposed to originally be based on
# https://github.com/raspberrypi/picamera2/blob/main/examples/mjpeg_server.py
# which is based on https://picamera.readthedocs.io/en/release-1.13/recipes2.html
# But like, having 640x480 snapshots is crappy and there's no support for doing
# switch_mode_and_capture_request while using start_recording so things are getting ugly.

import os
from pathlib import Path
import io
import logging
import socketserver
from http import server
import threading
import time
import simplejpeg
from pprint import pprint

def setup_libcamera_symlink():
    venv = Path(os.environ['VIRTUAL_ENV'])
    # ln -s /usr/lib/python3.12/site-packages/libcamera/ \
    # .venv/lib/python3.12/site-packages/libcamera
    package_path = venv / 'lib/python3.12/site-packages/libcamera'
    target = Path('/usr/lib/python3.12/site-packages/libcamera/')
    assert target.exists(), "need to install libcamera"
    print(package_path, target)
    if not package_path.exists():
        package_path.symlink_to(target)
        print(package_path, target)
setup_libcamera_symlink()

import libcamera, picamera2
from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder, JpegEncoder
from picamera2.outputs import FileOutput

# Size of live MJPG stream. Pi Camera 3 is 4608x2592, a 16:9 aspect ratio
STREAM_SIZE = (640, 480)
# STREAM_SIZE = (1920, 1080)

# Note that OctoPrint is set up with haproxy proxying content from port 8080 to http://host/webcam by default
# PORT = 8070
PORT = 8080

# libcamera.Transform(rotation: int = 0, hflip: bool = False, vflip: bool = False, transpose: bool = False)
TRANSFORM = libcamera.Transform()
# TRANSFORM = Transform(rotation=180)

# The camera likes to crop the image to get to STREAM_SIZE.
# See
#   https://picamera.readthedocs.io/en/release-1.13/fov.html#camera-modes (this is for the old picamera lib)
#   https://github.com/raspberrypi/picamera2/issues/450
#
# So here we tell it the exact size of the sensor mode to use so we can scale down from there.
# Run `libcamera-hello --list-cameras` to get a list of supported modes.

# SENSOR_MODE = (2304, 1296) # 2:2 binned mode for Pi Camera 3
# SENSOR_MODE = (1920, 1080)
# SENSOR_MODE = (640, 480)
SENSOR_MODE = STREAM_SIZE
# SENSOR_MODE = (2592, 1944)

CONTROLS = {
    # See https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf under "Appendix C: Camera controls"

    # "AfMode": libcamera.controls.AfModeEnum.Continuous,
    "ExposureValue": 0, # from -8 to 8

    # Specify the cropping+scaling in sensor pixels.
    # "ScalerCrop": [0, 0, 4608, 2592],
}




# https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf#%5B%7B%22num%22%3A70%2C%22gen%22%3A0%7D%2C%7B%22name%22%3A%22XYZ%22%7D%2C115%2C841.89%2Cnull%5D

def gen_index_page():
    global picam2
    return f"""
<!doctype HTML>
<html>
<head>
    <title>libcamera-streamer</title>
    <style>
        html, body {{ margin: 0; padding: 0; color: white; background: #555; }}
        p {{ margin: 10px; }}
        a {{ color: deepskyblue; }}
    </style>
</head>
<body>
    <p>
        Native resolution: {str(picam2.sensor_resolution)}<br>
        Native color: {picam2.sensor_format}<br>
        Attached camera:
    </p>
    <img src="stream.mjpg" width="{STREAM_SIZE[0]}" height="{STREAM_SIZE[1]}" />
    <br>
    <p>
        <a href="snapshot.jpg">High-resolution snapshot</a>
    </p>
</body>
</html>
"""


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


class StreamingHandler(server.BaseHTTPRequestHandler):
    def _cache_me_not(self):
        self.send_header('Age', 0)
        self.send_header('Cache-Control', 'no-cache, private')
        self.send_header('Pragma', 'no-cache')

    def do_GET(self):


        if self.path == '/':
            page_data = gen_index_page().encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', len(page_data))
            self._cache_me_not()
            self.end_headers()
            self.wfile.write(page_data)
        elif self.path == '/stream.mjpg':
            self.send_response(200)
            self._cache_me_not()
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
            self.end_headers()
            self._stream_jpeg()
        elif self.path == '/snapshot.jpg':
            self._send_snapshot()
        else:
            self.send_error(404)
            self.end_headers()

    def _stream_jpeg(self):
        global connection_count
        with picam2_lock:
            connection_count += 1
            if connection_count >= 1:
                picam2.start()
                picam2.start_encoder(jpeg_encoder[0], jpeg_encoder[1])
        try:
            while True:
                with jpeg_output.condition:
                    jpeg_output.condition.wait()
                    frame = jpeg_output.frame
                self.wfile.write(b'--FRAME\r\n')
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', len(frame))
                self.end_headers()
                self.wfile.write(frame)
                self.wfile.write(b'\r\n')
        except Exception as e:
            logging.warning(
                'Removed streaming client %s: %s',
                self.client_address, str(e)
            )
        finally:
            with picam2_lock:
                connection_count -= 1
                if connection_count <= 0:
                    picam2.stop_encoder()
                    picam2.stop()

    def _send_snapshot(self):
        self.send_response(200)

        log.info("will get image")

        with picam2_lock:
            log.info("got lock")

            picam2.start() # does nothing if camera is already running
            # pause usual preview stream to grab a full-resolution image
            picam2.stop_encoder()


            # log.info("request image")
            # request = picam2.switch_mode_capture_request_and_stop(camera_config_snapshot)
            # log.info("got image")
            # img_data = request.make_array("main")
            # img_format = request.config["main"]["format"]
            # request.release()

            log.info("request image")
            img_data = picam2.switch_mode_and_capture_array(camera_config_snapshot)
            log.info("got image")

            # picam2.configure(camera_config_stream)
            if connection_count <= 0:
                picam2.stop()
            else:
                picam2.start_encoder(jpeg_encoder[0], jpeg_encoder[1])
            log.info("resume record")

        log.info("start encode")
        jpeg_data = simplejpeg.encode_jpeg(
            img_data, quality=80,
            colorspace=JpegEncoder.FORMAT_TABLE[camera_config_snapshot["main"]["format"]],
            colorsubsampling="420",
        )
        log.info("encoded")

        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', len(jpeg_data))
        self._cache_me_not()
        self.end_headers()
        self.wfile.write(jpeg_data)


class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

def main():
    global log
    log = logging.getLogger(__name__)
    logging.basicConfig(level=logging.DEBUG)
    # logging.getLogger("picamera2", logging.INFO)

    log.info("Checking camera")

    global connection_count
    connection_count = 0

    global picam2
    picam2 = Picamera2()

    raw_mode = None
    for mode in picam2.sensor_modes:
        if mode["size"] == SENSOR_MODE: raw_mode = mode

    if not raw_mode:
        pprint(picam2.sensor_modes)
        raise Exception("Failed to find a matching sensor mode")


    camera_config_stream = picam2.create_video_configuration(
        main={"size": STREAM_SIZE},
        raw={
            "size": raw_mode["size"],
            "format": raw_mode["format"],
        },
    )
    camera_config_stream["transform"] = TRANSFORM

    global camera_config_snapshot
    camera_config_snapshot = picam2.create_still_configuration(main={"size": SENSOR_MODE})
    camera_config_snapshot["transform"] = TRANSFORM


    picam2.configure(camera_config_stream)
    picam2.set_controls(CONTROLS)
    # picam2.start()

    global jpeg_output
    global jpeg_encoder
    jpeg_output = StreamingOutput()
    jpeg_encoder = (MJPEGEncoder(), FileOutput(jpeg_output)) # 22% CPU at 640x480 on my Pi 4, maybe lower qualities around 20%
    # jpeg_encoder = (JpegEncoder(), FileOutput(jpeg_output)) # ~46% CPU on my Pi 4
    # picam2.start_encoder(jpeg_encoder[0], jpeg_encoder[1])

    global picam2_lock
    picam2_lock = threading.Lock()
    log.info("Entering main loop")
    try:
        address = ('', PORT)
        server = StreamingServer(address, StreamingHandler)
        server.serve_forever()
    finally:
        with picam2_lock:
            picam2.stop_recording()

