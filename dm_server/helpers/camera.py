"""DM helpers for camera acquisition.

All functions take a *params* dict and return either:
  - dict                      (no image)
  - (dict, numpy.ndarray)     (with image)

Camera functions are invoked through script strings and image arrays are read
back through the microscope software's Python image API.
"""

import numpy as np
import DigitalMicrograph as DM

from dm_server.helpers._dm_util import safe_exec_script, read_float, read_string

# Maps processing string -> DM-Script expression that returns the enum value
_PROC_MAP = {
    "unprocessed":     "CameraGetUnprocessedEnum()",
    "dark_subtracted": "CameraGetDarkSubtractedEnum()",
    "gain_normalised": "CameraGetGainNormalizedEnum()",
}


def list_cameras(params: dict) -> dict:
    """Return a list of all available cameras.

    Returns
    -------
    {"cameras": [{"id": int, "name": str}, ...]}
    """
    safe_exec_script(
        """\
        number n = CameraGetNumberOfCameras()
        TagGroup tg = GetPersistentTagGroup()
        TagGroupSetTagAsFloat(tg, "__amadeus__:cam:count", n)
        number i
        for (i = 0; i < n; i++) {
            TagGroupSetTagAsString(tg, "__amadeus__:cam:name:" + i, CameraGetName(i))
        }
        """,
        expected_tags=["cam:count"],
    )
    n = int(read_float("cam:count"))
    cameras = []
    for i in range(n):
        name = read_string(f"cam:name:{i}")
        cameras.append({"id": i, "name": name})
    return {"cameras": cameras}


def get_camera_info(params: dict) -> dict:
    """Return info about a camera.

    Parameters
    ----------
    params["camera_id"] : int, optional
        If omitted, uses the active camera.

    Returns
    -------
    {
        "camera_id": int,
        "name": str,
        "width": int,
        "height": int,
        "pixel_size_um": {"x": float, "y": float},
    }
    """
    if "camera_id" in params:
        cam_expr = str(int(params["camera_id"]))
    else:
        cam_expr = "CameraGetActiveCameraID()"

    safe_exec_script(
        f"""\
        number cam_id = {cam_expr}
        number sx, sy, psx, psy
        CameraGetSize(cam_id, sx, sy)
        CameraGetPixelSize(cam_id, psx, psy)
        TagGroup tg = GetPersistentTagGroup()
        TagGroupSetTagAsFloat(tg, "__amadeus__:cam:id",  cam_id)
        TagGroupSetTagAsFloat(tg, "__amadeus__:cam:sx",  sx)
        TagGroupSetTagAsFloat(tg, "__amadeus__:cam:sy",  sy)
        TagGroupSetTagAsFloat(tg, "__amadeus__:cam:psx", psx)
        TagGroupSetTagAsFloat(tg, "__amadeus__:cam:psy", psy)
        TagGroupSetTagAsString(tg, "__amadeus__:cam:name:active", CameraGetName(cam_id))
        """,
        expected_tags=["cam:sx", "cam:sy"],
    )
    cam_id = int(read_float("cam:id"))
    return {
        "camera_id": cam_id,
        "name":       read_string("cam:name:active"),
        "width":      int(read_float("cam:sx")),
        "height":     int(read_float("cam:sy")),
        "pixel_size_um": {
            "x": read_float("cam:psx"),
            "y": read_float("cam:psy"),
        },
    }


def camera_acquire(params: dict):
    """Acquire a single TEM camera frame.

    Parameters
    ----------
    params["exposure_s"] : float, default 1.0
        Exposure time in seconds.
    params["binning"] : int, default 1
        Pixel binning (applied equally to x and y).
    params["processing"] : str, default "gain_normalised"
        One of "unprocessed", "dark_subtracted", "gain_normalised".
    params["camera_id"] : int, optional
        Camera ID.  If omitted, uses CameraGetActiveCameraID().
    params["area"] : [top, left, bottom, right], optional
        ROI in pixels.  If omitted, acquires the full frame.

    Returns
    -------
    (dict, numpy.ndarray)
        dict — metadata including image_meta with calibration.
        numpy.ndarray — 2-D float32 pixel array.
    """
    exposure_s  = float(params.get("exposure_s",  1.0))
    binning     = int(params.get("binning",   1))
    proc_key    = params.get("processing", "gain_normalised")
    area        = params.get("area")  # [top, left, bottom, right] or None

    if proc_key not in _PROC_MAP:
        raise ValueError(
            f"Unknown processing {proc_key!r}. Choose from: {list(_PROC_MAP)}"
        )
    proc_expr = _PROC_MAP[proc_key]

    if "camera_id" in params:
        cam_expr = str(int(params["camera_id"]))
    else:
        cam_expr = "CameraGetActiveCameraID()"

    if area is not None:
        t, l, b, r = [int(v) for v in area]
        acquire_call = (
            f"CameraAcquire(cam_id, {exposure_s}, {binning}, {binning}, proc,"
            f" {t}, {l}, {b}, {r})"
        )
    else:
        acquire_call = f"CameraAcquire(cam_id, {exposure_s}, {binning}, {binning}, proc)"

    # DigiScan-busy guard.  If a DS acquisition (e.g. continuous STEM view) is
    # still running, CameraPrepareForAcquire raises "Acquisition already in
    # progress!".  Force-stop any DS acquisition and poll until it has cleared
    # before touching the camera.  Bounded by ~5 s; if DS is still active
    # after that, fall through and let CameraPrepareForAcquire raise.
    safe_exec_script(
        f"""\
        number cam_id = {cam_expr}
        number proc   = {proc_expr}
        if ( DSIsAcquisitionActive() )
        {{
            DSStopAcquisition(-1)
        }}
        number _wait_n = 0
        while ( DSIsAcquisitionActive() && _wait_n < 50 )
        {{
            Sleep(0.1)
            _wait_n = _wait_n + 1
        }}
        CameraPrepareForAcquire(cam_id)
        image img := {acquire_call}
        ShowImage(img)
        number origin_x, scale_x, origin_y, scale_y
        string units_x
        img.ImageGetDimensionCalibration(0, origin_x, scale_x, units_x, 1)
        img.ImageGetDimensionCalibration(1, origin_y, scale_y, units_x, 1)
        number sx = img.ImageGetDimensionSize(0)
        number sy = img.ImageGetDimensionSize(1)
        TagGroup tg = GetPersistentTagGroup()
        TagGroupSetTagAsFloat(tg, "__amadeus__:cam:id",    cam_id)
        TagGroupSetTagAsFloat(tg, "__amadeus__:cam:sx",    sx)
        TagGroupSetTagAsFloat(tg, "__amadeus__:cam:sy",    sy)
        TagGroupSetTagAsFloat(tg, "__amadeus__:cam:scx",   scale_x)
        TagGroupSetTagAsFloat(tg, "__amadeus__:cam:scy",   scale_y)
        TagGroupSetTagAsString(tg, "__amadeus__:cam:units", units_x)
        TagGroupSetTagAsString(tg, "__amadeus__:cam:name:active", CameraGetName(cam_id))
        """,
        expected_tags=["cam:sx", "cam:sy", "cam:scx"],
    )

    img = DM.FindFrontImage()
    arr = np.array(img.GetNumArray()).astype(np.float32)

    cam_id   = int(read_float("cam:id"))
    cam_name = read_string("cam:name:active")
    x_scale  = read_float("cam:scx")
    y_scale  = read_float("cam:scy")
    units    = read_string("cam:units")

    meta = {
        "camera_id":   cam_id,
        "camera_name": cam_name,
        "exposure_s":  exposure_s,
        "binning":     binning,
        "processing":  proc_key,
        "image_meta": {
            "name": cam_name,
            "calibration": {
                "x_scale": x_scale,
                "y_scale": y_scale,
                "units":   units,
            },
        },
    }
    return meta, arr
