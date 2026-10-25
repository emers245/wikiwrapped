/* The Help viewer: app/static/help.html in a frame over the dashboard.

   help.html is the one place the user help is written; this only displays
   it. A frame rather than copying the page in, so the help keeps its own
   stylesheet (the dashboard's lays the page out as an app grid) and also
   reads correctly when opened on its own ("Open in browser").

   WWhelp.open("takeout") jumps to a section. setup.js calls it for its
   help links, and the overlay sits above the Settings overlay so those
   links open on top of it rather than behind. */

window.WWhelp = (function () {
  const overlay = document.getElementById("help");
  const frame = document.getElementById("help-frame");
  const external = document.getElementById("help-external");
  const PAGE = "/static/help.html";

  function open(anchor) {
    const target = PAGE + (anchor ? "#" + anchor : "");
    external.href = target;
    // Show first: a frame inside a display:none overlay does not scroll,
    // so navigating before showing would leave it at the wrong place.
    overlay.classList.remove("hidden");
    document.getElementById("help-close").focus();

    let page = null;
    try {
      if (frame.contentWindow.location.pathname === PAGE) page = frame.contentWindow;
    } catch (err) {
      /* not loaded yet */
    }
    if (!page) {
      frame.src = target;        // first open: the browser scrolls to the hash
      return;
    }
    // Already loaded: scroll explicitly. Setting location.hash would do
    // nothing when it already holds this anchor (the same link twice).
    const section = anchor && page.document.getElementById(anchor);
    if (section) section.scrollIntoView();
    else page.scrollTo(0, 0);
    page.history.replaceState(null, "", target);
  }

  function close() {
    overlay.classList.add("hidden");
  }

  document.getElementById("btn-help").addEventListener("click", () => open(""));
  document.getElementById("help-close").addEventListener("click", close);
  overlay.addEventListener("click", event => {
    if (event.target === overlay) close();      // a click outside the card
  });
  window.addEventListener("keydown", event => {
    if (event.key === "Escape" && !overlay.classList.contains("hidden")) {
      event.stopPropagation();
      close();
    }
  }, true);

  return { open, close };
})();
