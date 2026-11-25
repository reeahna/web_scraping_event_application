# Safari checklist

The CI browser jobs run WebKit, Safari's engine, on Linux. That catches most
Safari problems but not the ones that come from iOS and macOS themselves: the
real share sheet, the iPhone keyboard, the collapsing address bar, the notch,
text size settings and Safari's own fonts. This checklist covers those. Run it
on the live site once on an iPhone and once on a Mac, and again after a change
to the header, the event page, the town page or the sign-in page.

Each pass takes about 15 minutes. Steps marked **(reads only)** change
nothing. Steps marked **(optional: changes your account)** save, follow or sign
in with your own account; skip them to keep the pass read-only. No step changes
anything for other visitors.

Write down the device, iOS or macOS version, and anything that looks wrong,
with a screenshot.

## iPhone (Safari)

Open the live site in Safari, not in an app's built-in browser.

### Home and town search (reads only)

- [ ] The page fits the screen: nothing is cut off at the right edge and the page does not move sideways when you drag it.
- [ ] Tap the town search box. The page does **not** zoom in (Safari zooms when a field's text is smaller than 16px).
- [ ] The keyboard does not cover the search box or the suggestions.
- [ ] Type part of a town name. Suggestions appear on the first letters; tap one and its town page opens.
- [ ] Type a school name (for example "Indiana"). Its town is offered.

### Town page (reads only)

- [ ] Scroll down slowly. The header stays at the top and nothing jumps when Safari's address bar shrinks or grows.
- [ ] Tap Filters. The panel opens; pick a category and tap Apply filters. Results change and the panel stays open.
- [ ] Tap a date field. The iOS date picker appears and the chosen date is used.
- [ ] Tap Clear filters. All events come back.
- [ ] Turn the phone sideways. The page still fits beside the notch or Dynamic Island, and nothing hides behind it.

### Event page (reads only)

- [ ] Event times, venue and address read correctly and nothing overlaps.
- [ ] Tap **Share…**. The iOS share sheet opens with the event's title and link. Close it without sharing.
- [ ] Tap **Copy link**. It says "Link copied". Paste into Notes: the link is the event's address.
- [ ] Tap View original listing. It opens in a new tab; go back with the back gesture.
- [ ] Use the back button to return to the town page. Your filters and scroll position are still there, and no menu is stuck open.

### Settings that change the layout (reads only)

- [ ] Safari's **aA** button: set the page to 150%. Pages still fit and buttons are still tappable.
- [ ] iPhone Settings > Display > Text Size larger: headings and buttons do not overlap.
- [ ] Dark Mode on: text is readable everywhere (header, filters, event cards, sign-in page).
- [ ] A Private Browsing tab: the home page, a town and an event all load.

### Signed in (optional: changes your account)

- [ ] Tap Sign in and sign in. You come back to the page you started from.
- [ ] Open the My Account menu. It opens on tap and closes when you tap elsewhere.
- [ ] Save an event, then unsave it.
- [ ] Log out. The header shows Sign in again.

### Admin on the phone (reads only)

- [ ] Open /admin. The Menu button shows the admin sections; each section opens on tap, and only one is open at a time.
- [ ] Websites and Events lists: you can reach every column (the table scrolls inside its own box) and the action buttons are tappable.
- [ ] Do not press any action button during this pass.

## Mac (Safari)

### Public pages (reads only)

- [ ] Home, a town page and an event page look right at full width and with the window narrowed to about phone width.
- [ ] Town search: type, use the arrow keys to pick a suggestion, press Return.
- [ ] Tab through the town page with the keyboard (turn on Safari Settings > Advanced > "Press Tab to highlight each item on a webpage" first). Every link and button shows a visible focus outline and nothing focused is hidden under the header.
- [ ] Event page: **Share…** opens the macOS share menu; **Copy link** copies the address.
- [ ] Zoom in with Command-plus to 200%. Pages reflow without sideways scrolling.

### Signed in (optional: changes your account)

- [ ] Sign in and out once, as on the iPhone.
- [ ] My Account menu: opens on click, closes on Escape and on a click elsewhere.

### Admin (reads only)

- [ ] Hover each admin menu: it opens, and moving to another menu closes the first.
- [ ] Click a menu, then press Escape: it closes.
- [ ] Timestamps on the Websites list show your own time zone.

## When something is wrong

Note the page address, the device and the step, with a screenshot, and pass it
on to be fixed. If the same problem also appears in the WebKit CI job's
screenshots, it is a layout bug a test can catch; otherwise it is Safari-only
and goes on this checklist's next run.
