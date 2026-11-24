# DigitalMicrograph Python Scripting: Architecture & API Analysis

## Overview

This document analyzes the DigitalMicrograph (GMS) Python scripting API — its design patterns, level of abstraction, and suitability as a foundation for building a Model Context Protocol (MCP) server or RESTful API for remote microscope control.

The analysis is based on the official Python help documentation (`Python.md`), the SWIG-generated Python helpfile (`DigitalMicrograph_python_helpfile.txt`), and the DM scripting reference (`Scripting_DigitalMicrograph.md`).

---

## 1. How the API is Built

The Python API is an **auto-generated SWIG 4.1.1 wrapper** around Gatan's internal C++ class library. It is not a purpose-built Python SDK — it is a mechanical translation of C++ interfaces into Python bindings. The module is imported as:

```python
import DigitalMicrograph as DM
```

### Scale

- **11 primary classes** (all prefixed `Py_`)
- **~80 module-level functions** in the `DigitalMicrograph` namespace
- **~415 class methods**
- **~495+ total callable functions/methods**

### Class Hierarchy

```
DigitalMicrograph (module)
├── Py_Image             (~60 methods)  — Core image data object
├── Py_ImageDocument     (~30 methods)  — Document container for images
├── Py_ImageDisplay      (~50 methods)  — Display/rendering control
│   └── Py_LinePlotImageDisplay         — Line plot specialization
├── Py_Component         (~50 methods)  — Annotations and graphics
├── Py_Window            (~20 methods)  — Window management
├── Py_TagGroup          (~100 methods) — Hierarchical metadata (tags)
├── Py_ROI               (~30 methods)  — Regions of interest
├── Py_Camera            (~16 methods)  — Detector control & acquisition
├── Py_Microscope        (~80 methods)  — Microscope hardware control
├── Py_ScriptObject      (~60 methods)  — Event handling / listeners
└── DS_* functions       (~40 funcs)    — DigiScan (STEM scanning)
```

### Naming Conventions

The API follows consistent C++-style naming throughout:

| Pattern | Example | Purpose |
|---|---|---|
| `GetXXX()` | `GetFocus()`, `GetName()` | Property getter |
| `SetXXX()` | `SetFocus(30)`, `SetName("img")` | Property setter |
| `IsXXX()` / `CanGetXXX()` | `IsValid()`, `CanGetCameraLength()` | Boolean capability check |
| `CountXXX()` | `CountChildren()`, `CountROIs()` | Collection size |
| `NewXXX()` / `CreateXXX()` | `NewTagGroup()`, `CreateImage()` | Object factory |
| `HandleXXXEvent()` | `HandleDataChangedEvent()` | Event subscription |
| `DS_XXX()` | `DS_StartAcquisition()` | DigiScan functions |

---

## 2. Functional Domains Covered

### Fully Exposed in Python

| Domain | Key Classes/Functions | Quality |
|---|---|---|
| **Image Data** | `Py_Image.GetNumArray()` — direct NumPy shared-memory mapping | Excellent |
| **Metadata/Tags** | `Py_TagGroup` — works as Python dict/list with `[]`, `keys()`, `len()`, iteration | Excellent |
| **Camera Acquisition** | `Py_Camera` — single-shot, in-place, and continuous acquisition modes | Good |
| **Microscope Control** | `Py_Microscope` — focus, beam shift/tilt, magnification, stigmation, high tension, camera length, illumination/imaging modes | Good |
| **STEM Scanning** | `DS_*` functions — parameter sets, signal routing, beam control | Good |
| **Display Properties** | `Py_ImageDisplay` — contrast, color tables, gamma, captions | Good |
| **Annotations** | `Py_Component` — lines, arrows, boxes, ovals, text with color/font control | Good |
| **ROIs** | `Py_ROI` — point, line, rectangle, oval, circle, range, polygon | Good |
| **Document/Window** | `Py_ImageDocument`, `Py_Window` — workspace and window management | Adequate |
| **Event System** | `Py_ScriptObject` — listeners for image, display, ROI, and component changes | Good |

### NOT Exposed in Python (DM-Script Only)

| Domain | Notes |
|---|---|
| **Image Processing** | FFT, filtering, convolutions, sharpening — hundreds of functions |
| **Image Analysis** | Histograms, particle analysis, statistics functions |
| **EELS/EDS Analysis** | Spectroscopy-specific processing and quantification |
| **RGB Image Creation** | No `RGB()` function — requires hybrid DM-script workaround |
| **Fourier Processing** | Masking, auto/cross-correlation |
| **File Format Support** | Limited; DM-script has broader import/export |

These gaps can be bridged using `DM.ExecuteScriptString()` to run DM-script from Python, but this requires fragile string templating.

---

## 3. Abstraction and Obfuscation Assessment

### Well-Designed / Low Friction

- **NumPy integration**: `img.GetNumArray()` returns a NumPy array that shares memory with the DM image. Modifying the array modifies the image data directly. This is the API's strongest feature.
- **TagGroup as dict**: Pythonic access to metadata — `tags['key'] = value`, iteration, `keys()`, nested group support.
- **Camera workflow**: Clean acquire/display cycle in 3-4 lines of code.
- **Microscope Get/Set pairs**: Straightforward — `mic.GetFocus()` / `mic.SetFocus(30)`.

### Awkward / Obfuscated

- **Magic integer enums**: Component types are undocumented integers (2=line, 3=arrow, 4=double arrow, 5=box, 6=oval, 33=intensity bar). Processing modes are bare ints.
- **Dimension inversion**: NumPy array shapes are reversed relative to DM image dimensions (DM `100x20` = NumPy shape `(20, 100)`). Documented but error-prone.
- **Manual memory management**: All `Py_Image` variables must be explicitly `del`'d or they leak memory. This is a SWIG artifact that breaks Python conventions.
- **No discovery/introspection**: No way to enumerate available microscope capabilities, supported camera features, or valid parameter ranges programmatically. Must use `CanGetXXX()` guard methods.
- **Single-script execution**: Only one Python script can run at a time. A second script attempt produces an error dialog.
- **Thread complexity**: Some operations must run on the DM main thread (blocking UI), others on the Python background thread. Matplotlib specifically requires the DM main thread. No clear documentation of per-method thread requirements.
- **Overloaded methods via `*args`**: SWIG maps C++ overloads to Python `*args`, losing type safety and IDE support. For example, `AcquireImage()` has 5 different signatures selected by argument count.

### Overall Abstraction Level: Moderate

The API is a **thin, mechanical wrapper** — it faithfully mirrors internal C++ object interfaces without reshaping them for external consumption. It feels more like a "leaked internal SDK" than a designed-for-developers public API. The NumPy integration and TagGroup Pythonic access are notable exceptions where real design effort was applied.

---

## 4. Standalone Python Support (New in Recent GMS)

A critical recent addition is **standalone Python support**, documented at the end of `Python.md`. This allows external Python interpreters (CPython, Anaconda, Jupyter) to interface with a running DigitalMicrograph instance via a **ZMQ/JSON message-passing bridge**.

Key details:

- Implemented through the `GMSExternalPython` module
- Communicates over a configurable RPC port (default: 5557)
- Supports both **local** (same machine) and **remote** (same network) operation
- Provides access to Image, TagGroup, Camera, and DigiScan APIs
- Supports hybrid scripting (execute DM-script, pass Image/TagGroup objects)
- **Does NOT expose**: Display-related APIs (ROI, ImageDisplay, LinePlotImageDisplay)
- Configuration: `Private:Python:ExternalPython:Connection:rpc_port` in DM global tags, matched by `config.yaml` in the GMSExternalPython module

This standalone bridge is architecturally significant — it decouples the Python client from DM's single-script execution constraint and provides the network transport layer that a REST or MCP server would otherwise need to build from scratch.

---

## 5. Suitability for Building an MCP Server

### Natural Mappings

MCP tools are command-oriented, which maps well to the DM Python API's Get/Set/Acquire patterns:

| MCP Tool Concept | DM Python Mapping |
|---|---|
| `acquire_image` | `cam.AcquireImage(exposure, bin, bin, processing)` |
| `get_microscope_state` | `mic.GetFocus()`, `mic.GetMagnification()`, etc. |
| `set_microscope_param` | `mic.SetFocus(val)`, `mic.SetBeamShift(x, y)`, etc. |
| `get_image_metadata` | `img.GetTagGroup()` → serialize as JSON dict |
| `get_image_data` | `img.GetNumArray()` → serialize as base64/PNG |
| `start_stem_scan` | `DS_CreateParameters()` → `DS_StartAcquisition()` |
| `list_images` | Iterate via `FindImageByID()` or `CountImageDocuments()` |
| `run_dm_script` | `DM.ExecuteScriptString(script)` — escape hatch for anything not in Python API |

### Strengths for MCP

- **TagGroup → JSON**: TagGroups are hierarchical key-value stores that map directly to JSON. Metadata round-trips cleanly.
- **NumPy → binary**: Image data as NumPy arrays can be efficiently serialized (base64, PNG, TIFF, raw bytes).
- **Event system**: `Py_ScriptObject` listeners could drive MCP notifications/resources for live updates.
- **Standalone ZMQ bridge**: Already provides network transport and process isolation.
- **`ExecuteScriptString()` escape hatch**: Any DM capability not in the Python API can still be reached by constructing and executing DM-script strings.

### Challenges for MCP

| Challenge | Severity | Mitigation |
|---|---|---|
| **Single-script execution** | High | Use standalone Python/ZMQ bridge which manages this internally |
| **Missing image processing** | Medium | Template DM-script strings via `ExecuteScriptString()` |
| **Stateful object model** | Medium | Track resource IDs (image IDs, ROI IDs) in MCP server state |
| **Memory leak risk** | Medium | Ensure all handlers `del` Py_Image vars; use try/finally |
| **Thread management** | Medium | Route requests to appropriate thread; standalone bridge handles this |
| **Large binary transfers** | Low-Medium | Chunk or stream large images; support binning/ROI for smaller transfers |
| **No RGB creation** | Low | Use hybrid scripting workaround |

---

## 6. Suitability for Building a REST API

### Resource Model

The DM object model maps reasonably well to REST resources:

```
GET    /microscope                    → current microscope state
GET    /microscope/focus              → mic.GetFocus()
PUT    /microscope/focus              → mic.SetFocus(value)
GET    /microscope/magnification      → mic.GetMagnification()
PUT    /microscope/magnification      → mic.SetMagnification(value)

GET    /cameras                       → list available cameras
GET    /cameras/{id}                  → camera info (name, size, pixel size)
POST   /cameras/{id}/acquire          → cam.AcquireImage(params)

GET    /images                        → list open images
GET    /images/{id}                   → image metadata
GET    /images/{id}/data              → image pixel data (NumPy → binary)
GET    /images/{id}/tags              → img.GetTagGroup() → JSON
PUT    /images/{id}/tags/{path}       → set tag value

POST   /digiscan/acquire              → DS_CreateParameters + DS_StartAcquisition
GET    /digiscan/status               → scan status

POST   /scripts/execute               → DM.ExecuteScriptString(body)
```

### REST-Specific Challenges

- **Statelessness**: DM is deeply stateful (images live in workspace, displays tied to windows). REST would need resource IDs and lifecycle management.
- **Long-running operations**: Acquisitions and scans take time. Need async patterns (polling endpoint or WebSocket).
- **Binary payloads**: Image data doesn't fit neatly into JSON. Need content negotiation or multipart responses.

---

## 7. Recommended Architecture

The optimal architecture for either MCP or REST uses the **standalone Python ZMQ bridge** as the transport layer:

```
┌──────────────┐     ┌─────────────────────┐     ┌─────────────────────┐
│              │     │                     │     │                     │
│  MCP/REST    │────▶│  GMSExternalPython  │────▶│  DigitalMicrograph  │
│  Client      │     │  (ZMQ/JSON bridge)  │     │  (GMS Application)  │
│              │◀────│  Port 5557          │◀────│                     │
└──────────────┘     └─────────────────────┘     └─────────────────────┘
```

This approach:
- Avoids the single-script execution limitation
- Provides process isolation (server crash doesn't crash DM)
- Supports remote operation out of the box
- Handles thread management internally
- Gives access to the core APIs (Image, TagGroup, Camera, DigiScan, hybrid scripting)

The MCP or REST server layer would be a relatively thin Python service that:
1. Accepts client requests
2. Translates them to GMSExternalPython calls
3. Serializes responses (JSON for metadata, binary for image data)
4. Manages resource lifecycles and error handling

---

## 8. Feasibility Summary

| Approach | Feasibility | Notes |
|---|---|---|
| **MCP server via standalone Python/ZMQ** | **High** | Best path. MCP tools map naturally to Get/Set/Acquire operations. ZMQ bridge handles threading and process isolation. |
| **REST API via standalone Python/ZMQ** | **High** | Same ZMQ backbone. REST resources map to microscope/camera/image objects. Needs async pattern for long operations. |
| **MCP/REST via embedded Python** | **Medium** | Must serialize all requests through single execution context. Workable but fragile. |
| **Full image processing via API** | **Low-Medium** | Requires heavy use of `ExecuteScriptString()` for DM-script. String templating is brittle. |

### Bottom Line

The DM Python API covers **~60% of what's needed** for remote instrument control (microscope, camera, scanning, images, metadata). The remaining ~40% (image processing, analysis, some display features) requires falling through to DM-script string execution.

For an **MCP server**, this is a good fit — MCP tools are naturally command-oriented ("acquire image", "set focus", "get metadata"), which aligns well with the API's Get/Set/Acquire pattern. The standalone Python ZMQ bridge is the right foundation, and gaps can be covered with `ExecuteScriptString()`.

The API's main limitations — single-script execution, manual memory management, magic-number enums, missing image processing — are all manageable with appropriate server-side patterns. The biggest risk is stability under sustained automated use, as the API was designed for interactive scripting, not high-throughput programmatic access.
