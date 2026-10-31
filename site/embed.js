/* The tracker's article embed: builds the frame inside the block from embed/wordpress-embed.html, then sizes it to the table.
   It lives here, not in the article, because Inc42's app prints any script text in an article as words. It builds the frame
   because on no-sidebar articles the theme's server code moves an iframe tag out of the reading column.
   The block pins this file with an integrity hash, so any change here needs a new hash in every article. */
(function () {
  var box = document.getElementById("inc42-listed-tracker-box");
  if (!box) return;
  var src = box.querySelector("a").href;   // the tracker's address, from the block's "Open the tracker" link
  var origin = new URL(src).origin;
  var pages = 0;
  var HEADER = 100;   // the site's sticky header
  var frame = document.createElement("iframe");
  frame.id = "inc42-listed-tracker";
  frame.setAttribute("aria-label", "Inc42 Listed New-Age Tech Company Tracker: search, filter and sort every listed company");
  frame.setAttribute("loading", "lazy");
  frame.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
  frame.setAttribute("frameborder", "0");
  // The fallback height, until the widget reports its own
  frame.style.cssText = "display:block;width:100%;max-width:100%;height:5400px;margin:0;padding:0;border:0;";
  window.addEventListener("message", function (e) {
    if (e.origin !== origin || e.source !== frame.contentWindow) return;
    var d = e.data;
    if (!d) return;
    // The reader moved between the table and a profile inside the frame: bring the top of the new page into view
    if (d.type === "inc42-tracker-page") {
      var top = frame.getBoundingClientRect().top;
      if (pages++ && top < HEADER) window.scrollBy(0, top - HEADER);
      return;
    }
    // The widget reports its content height whenever it changes; the frame follows it
    if (d.type !== "inc42-tracker-height" || typeof d.height !== "number") return;
    frame.style.height = Math.max(400, Math.min(12000, Math.round(d.height))) + "px";
  });
  frame.src = src;
  box.insertBefore(frame, box.firstChild);
})();
