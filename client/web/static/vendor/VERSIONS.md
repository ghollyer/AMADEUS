# Vendored front-end libraries

Pinned copies so the console works on the instrument network with no
internet. No build step: each file is the package's own UMD bundle,
loaded as a classic script and read from `window`.

| File | Package | Version | Licence | Source |
|---|---|---|---|---|
| `htm-preact.umd.js` | htm (Preact standalone build) | 3.1.1 | Apache-2.0 / MIT (Preact) | https://cdn.jsdelivr.net/npm/htm@3.1.1/preact/standalone.umd.js |
| `dockview-core.min.js` | dockview-core (styles bundled) | 8.6.1 | MIT | https://cdn.jsdelivr.net/npm/dockview-core@8.6.1/dist/dockview-core.min.js |
| `blockly.min.js` | blockly (core + blocks + English) | 13.4.0 | Apache-2.0 | https://cdn.jsdelivr.net/npm/blockly@13.4.0/blockly.min.js |

To upgrade: download the same path at the new version, update this table,
and check the dockview `DockviewComponent` / Blockly field APIs used in
`js/app.js` and `js/flow_blocks.js`.
