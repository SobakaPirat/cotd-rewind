(function () {
  var tabs = document.querySelectorAll(".tab[data-tab]");
  if (!tabs.length) return;
  var panels = document.querySelectorAll(".panel[data-panel]");
  function activate(name) {
    tabs.forEach(function (tab) {
      var active = tab.getAttribute("data-tab") === name;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    panels.forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-panel") !== name;
    });
  }
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      activate(tab.getAttribute("data-tab"));
    });
  });
  var initial = document.querySelector(".tab.is-active");
  if (initial) activate(initial.getAttribute("data-tab"));
})();

(function () {
  var root = document.querySelector("[data-min-cotd]");
  if (!root) return;
  var label = root.querySelector("[data-min-label]");
  var buttons = root.querySelectorAll("[data-min]");
  // Номера строк — от видимого порядка: скрытые строки номер не занимают.
  function renumber(table) {
    if (!table.tBodies.length) return;
    var n = 0;
    Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
      var cell = row.querySelector(".cell-pos");
      if (!cell) return;
      if (row.hidden) { cell.textContent = ""; return; }
      n += 1;
      cell.textContent = String(n);
    });
  }
  function apply(min) {
    document.querySelectorAll("table.table").forEach(function (table) {
      if (!table.tBodies.length) return;
      Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
        var cell = row.querySelector('[data-key="cotd"]');
        var value = cell ? parseInt(cell.getAttribute("data-value"), 10) : 0;
        row.hidden = min > 0 && (isNaN(value) || value < min);
      });
      renumber(table);
    });
    buttons.forEach(function (button) {
      var value = parseInt(button.getAttribute("data-min"), 10);
      button.classList.toggle("is-current", value === min);
    });
    if (label) label.textContent = min > 0 ? "от " + min : "все";
    root.open = false;
  }
  buttons.forEach(function (button) {
    button.addEventListener("click", function () {
      apply(parseInt(button.getAttribute("data-min"), 10));
    });
  });
  var initial = parseInt(root.getAttribute("data-min-cotd"), 10);
  apply(isNaN(initial) ? 10 : initial);
})();

(function () {
  var pickers = document.querySelectorAll("details.year-pick");
  if (!pickers.length) return;
  document.addEventListener("click", function (event) {
    pickers.forEach(function (picker) {
      if (picker.open && !picker.contains(event.target)) picker.open = false;
    });
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    pickers.forEach(function (picker) { picker.open = false; });
  });
})();

(function () {
  function valueOf(row, key) {
    var cell = row.querySelector('[data-key="' + key + '"]');
    return cell ? cell.getAttribute("data-value") : null;
  }
  // Номера строк считаются от текущего порядка: после сортировки и фильтра
  // первый в списке — 1, скрытые строки номер не занимают.
  function renumber(table) {
    if (!table.tBodies.length) return;
    var n = 0;
    Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
      var cell = row.querySelector(".cell-pos");
      if (!cell) return;
      if (row.hidden) { cell.textContent = ""; return; }
      n += 1;
      cell.textContent = String(n);
    });
  }
  document.querySelectorAll("table.table").forEach(function (table) {
    var body = table.tBodies[0];
    table.tHead.querySelectorAll("th").forEach(function (th) {
      var key = th.getAttribute("data-key");
      if (!key) return; // колонка «№» не сортируется
      th.classList.add("sortable");
      th.addEventListener("click", function () {
        var ascending = th.getAttribute("data-order") !== "asc";
        table.tHead.querySelectorAll("th").forEach(function (other) {
          other.removeAttribute("data-order");
          other.classList.remove("is-sorted");
        });
        th.setAttribute("data-order", ascending ? "asc" : "desc");
        th.classList.add("is-sorted");
        var rows = Array.prototype.slice.call(body.rows);
        rows.sort(function (a, b) {
          var av = valueOf(a, key);
          var bv = valueOf(b, key);
          if (av === "" || av === null) return 1;
          if (bv === "" || bv === null) return -1;
          var an = parseFloat(av);
          var bn = parseFloat(bv);
          var result = (!isNaN(an) && !isNaN(bn))
            ? an - bn
            : String(av).localeCompare(String(bv), "ru");
          return ascending ? result : -result;
        });
        rows.forEach(function (row) { body.appendChild(row); });
        renumber(table);
      });
    });
    renumber(table);
  });
})();

(function () {
  var dots = document.querySelectorAll(".dot-hit");
  if (!dots.length) return;
  var tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.hidden = true;
  document.body.appendChild(tip);

  function show(dot, event) {
    var label = dot.getAttribute("data-label");
    if (!label) return;
    tip.textContent = label;
    tip.hidden = false;
    place(event);
  }
  function place(event) {
    var pad = 14;
    var x = event.pageX + pad;
    var y = event.pageY - tip.offsetHeight - 10;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }
  dots.forEach(function (dot) {
    var title = dot.querySelector("title");
    if (title) title.remove();  // чтобы не дублировать системную подсказку
    dot.addEventListener("mouseenter", function (event) { show(dot, event); });
    dot.addEventListener("mousemove", place);
    dot.addEventListener("mouseleave", function () { tip.hidden = true; });
    dot.setAttribute("tabindex", "0");
    dot.addEventListener("focus", function (event) { show(dot, event); });
    dot.addEventListener("blur", function () { tip.hidden = true; });
  });
})();