/*
 * Typeahead for the college-town chooser.
 *
 * The town list is embedded in the page as a JSON data block, so a suggestion
 * appears on the keystroke rather than after a round trip. External rather than
 * inline because the app's Content-Security-Policy is `script-src 'self'` with
 * no 'unsafe-inline' (app.config.Settings.content_security_policy).
 *
 * Progressive enhancement: the form is an ordinary GET and submits fine without
 * this file. The ARIA attributes in the markup start in their no-script state
 * and are only meaningful once this runs.
 */
(() => {
  const input = document.getElementById("city-search");
  const list = document.getElementById("city-suggestions");
  const source = document.getElementById("city-index");
  if (!input || !list || !source) return;

  let towns = [];
  try {
    towns = JSON.parse(source.textContent) || [];
  } catch {
    return; // Malformed data: leave the plain form working.
  }
  if (!towns.length) return;

  const MAX_SUGGESTIONS = 8;
  let matches = [];
  let active = -1;

  // Fold accents and case so "Malmo" finds "Malmö" and "ind" finds "Indiana".
  const fold = (value) =>
    (value || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();

  const search = (term) => {
    const needle = fold(term).trim();
    if (!needle) return [];
    const scored = [];
    for (const town of towns) {
      const fields = [town.name, town.school, town.state].map(fold);
      // A prefix match ranks above a match in the middle, so typing "ind"
      // offers Indiana University before a town that merely contains "ind".
      let rank = null;
      for (const field of fields) {
        if (!field) continue;
        if (field.startsWith(needle)) { rank = 0; break; }
        if (field.includes(needle)) rank = rank === null ? 1 : rank;
      }
      if (rank !== null) scored.push({ town, rank });
    }
    scored.sort((a, b) => a.rank - b.rank || a.town.name.localeCompare(b.town.name));
    return scored.slice(0, MAX_SUGGESTIONS).map((entry) => entry.town);
  };

  const close = () => {
    list.hidden = true;
    list.replaceChildren();
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  };

  const highlight = (index) => {
    active = index;
    [...list.children].forEach((item, i) => {
      const isActive = i === index;
      item.classList.toggle("is-active", isActive);
      item.setAttribute("aria-selected", String(isActive));
      if (isActive) {
        input.setAttribute("aria-activedescendant", item.id);
        item.scrollIntoView({ block: "nearest" });
      }
    });
    if (index < 0) input.removeAttribute("aria-activedescendant");
  };

  const render = () => {
    list.replaceChildren();
    matches.forEach((town, index) => {
      const item = document.createElement("li");
      item.id = `city-suggestion-${index}`;
      item.role = "option";
      item.className = "suggestion";
      item.setAttribute("aria-selected", "false");

      // textContent throughout: town and school names come from the database
      // and must never be parsed as markup.
      const name = document.createElement("span");
      name.className = "suggestion-name";
      name.textContent = town.state ? `${town.name}, ${town.state}` : town.name;
      item.appendChild(name);

      if (town.school) {
        const school = document.createElement("span");
        school.className = "suggestion-school";
        school.textContent = town.school;
        item.appendChild(school);
      }

      const count = document.createElement("span");
      count.className = "suggestion-count";
      count.textContent = `${town.count} event${town.count === 1 ? "" : "s"}`;
      item.appendChild(count);

      item.addEventListener("mousedown", (event) => {
        // mousedown, not click: the input's blur would close the list first.
        event.preventDefault();
        go(town);
      });
      list.appendChild(item);
    });

    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    highlight(-1);
  };

  const go = (town) => {
    window.location.href = `/city/${encodeURIComponent(town.slug)}`;
  };

  input.addEventListener("input", () => {
    matches = search(input.value);
    if (matches.length) render();
    else close();
  });

  input.addEventListener("keydown", (event) => {
    if (list.hidden || !matches.length) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      highlight((active + 1) % matches.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      highlight(active <= 0 ? matches.length - 1 : active - 1);
    } else if (event.key === "Enter" && active >= 0) {
      // Only when a suggestion is highlighted; otherwise the form submits and
      // the reader gets the full result page, which is also a valid answer.
      event.preventDefault();
      go(matches[active]);
    } else if (event.key === "Escape") {
      close();
    }
  });

  input.addEventListener("blur", () => window.setTimeout(close, 120));
  document.addEventListener("click", (event) => {
    if (!list.contains(event.target) && event.target !== input) close();
  });
})();
