/*
 * Show system timestamps in the viewer's own timezone.
 *
 * The server renders each one as
 *   <time datetime="2026-11-20T02:14:06Z" data-local-time>Nov 20, 2026 at 2:14 AM UTC</time>
 * (see admin_time_html in app/services/schedule_admin.py). This rewrites the
 * text into the browser's timezone, e.g. "Nov 19, 2026 at 9:14 PM EST", and
 * keeps the server text as a hover title. If the script does not run, the
 * server text (application timezone, labelled) is what shows.
 *
 * Event start and end times are not marked this way: they stay in the
 * event's town timezone.
 *
 * External file, not inline: the CSP is `script-src 'self'`.
 */
(() => {
  let formatter;
  try {
    formatter = new Intl.DateTimeFormat("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
      timeZoneName: "short",
    });
  } catch (err) {
    return;
  }

  const format = (date) => {
    const part = {};
    for (const { type, value } of formatter.formatToParts(date)) part[type] = value;
    const ampm = part.dayPeriod ? ` ${part.dayPeriod.toUpperCase()}` : "";
    const zone = part.timeZoneName ? ` ${part.timeZoneName}` : "";
    return `${part.month} ${part.day}, ${part.year} at ${part.hour}:${part.minute}${ampm}${zone}`;
  };

  const localize = (root) => {
    for (const el of root.querySelectorAll("time[data-local-time]")) {
      if (el.dataset.localized) continue;
      const date = new Date(el.getAttribute("datetime"));
      if (Number.isNaN(date.getTime())) continue;
      if (!el.title) el.title = el.textContent.trim();
      el.textContent = format(date);
      el.dataset.localized = "1";
    }
  };

  localize(document);
})();
