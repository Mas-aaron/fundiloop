The server fetches the esptool-js ESM bundle here automatically the
first time browser flash is used, then serves it locally (see README).
You should never need to touch this folder.

Manual fallback for fully offline machines: save the ESM build
(must export Transport + ESPLoader) as exactly this file:

  frontend/esptool-js/bundle.js

then reload the page. No server restart needed either way.
