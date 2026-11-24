// Запоминает отмеченные галочки в уроках (только в этом браузере).
(function () {
  function key() { return "progress:" + location.pathname; }
  function load() { try { return JSON.parse(localStorage.getItem(key()) || "{}"); } catch (e) { return {}; } }
  function save(state) { try { localStorage.setItem(key(), JSON.stringify(state)); } catch (e) {} }
  function init() {
    var boxes = document.querySelectorAll(".md-content .task-list-control input[type=checkbox]");
    if (!boxes.length) return;
    var state = load();
    boxes.forEach(function (box, i) {
      if (i in state) box.checked = state[i];
      box.addEventListener("change", function () { state[i] = box.checked; save(state); });
    });
  }
  if (window.document$) { window.document$.subscribe(init); } else { document.addEventListener("DOMContentLoaded", init); }
})();
