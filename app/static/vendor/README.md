# Vendored JavaScript libraries

Served from here rather than a CDN, so the app works offline and never
depends on a third-party host at runtime. The files are unmodified copies
of each package's published build.

| File | Package | Version | License | Source |
|---|---|---|---|---|
| `d3-7.9.0.min.js` | d3 | 7.9.0 | ISC (`LICENSE-d3.txt`) | npm tarball, `package/dist/d3.min.js` |
| `cytoscape-3.30.2.min.js` | cytoscape | 3.30.2 | MIT (`LICENSE-cytoscape.txt`) | npm tarball, `package/dist/cytoscape.min.js` |

d3 was previously loaded as `https://cdn.jsdelivr.net/npm/d3@7`, an
unpinned major version; 7.9.0 is what that URL resolved to on 2026-10-24,
so vendoring it changes nothing but pins it. cytoscape was already pinned
at 3.30.2 on unpkg.

How they were fetched (2026-10-24): each npm registry tarball was
downloaded and checked against the registry's published `sha512`
integrity before extraction, and the extracted file was compared byte for
byte with the CDN copy the app used before. SHA-256 of the vendored files:

```
f2094bbf6141b359722c4fe454eb6c4b0f0e42cc10cc7af921fc158fceb86539  d3-7.9.0.min.js
83e8c54a6bec655bfd81df07df605649c268af69aeca67a5ea2da54ea42dac81  cytoscape-3.30.2.min.js
```

To upgrade: fetch the new version the same way, add it under a new
versioned filename, update the `<script>` tags in `index.html` and
`network.html`, delete the old file, and update this table.
