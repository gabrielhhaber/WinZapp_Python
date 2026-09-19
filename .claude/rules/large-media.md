---
paths:
  - "client/core/wppconnect_sender_layer_patch.py"
  - "client/api_patches/src/controller/deviceController.ts"
  - "client/api_patches/src/util/functions.ts"
---

# Large media

**Read `docs/traps/large-media.md` before changing these files.** Short form:

Downloads are negotiated raw bytes (`Accept: application/octet-stream`) with a size-scaled timeout. Four size gates must agree (2 GB documents, 1 GB media). A shipped `_V<n>` patch constant is the left-hand side of a migration — add a version, never edit it.
