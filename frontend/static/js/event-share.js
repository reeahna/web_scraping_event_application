/*
 * Event sharing on the event page.
 *
 * Progressive enhancement: the platform links (X, Facebook, ...) are plain
 * links that work without this file. This only reveals the two buttons that
 * need the browser's help, and only where the browser can do them:
 *   - "Share…" opens the device's own share sheet (phones, Safari, Edge).
 *   - "Copy link" copies the event's address to the clipboard.
 *
 * External rather than inline because the CSP is `script-src 'self'`.
 */
(() => {
  const group = document.querySelector(".event-share");
  if (!group) return;

  const url = group.dataset.shareUrl;
  const title = group.dataset.shareTitle || document.title;
  const text = group.dataset.shareText || title;
  const status = group.querySelector(".share-status");
  const nativeButton = group.querySelector(".share-native");
  const copyButton = group.querySelector(".share-copy");

  const announce = (message) => {
    if (!status) return;
    status.textContent = "";
    // A fresh change so screen readers repeat the message on a second click.
    window.setTimeout(() => { status.textContent = message; }, 50);
  };

  const shareData = { title, text, url };
  if (nativeButton && navigator.share && (!navigator.canShare || navigator.canShare(shareData))) {
    nativeButton.hidden = false;
    nativeButton.addEventListener("click", async () => {
      try {
        await navigator.share(shareData);
      } catch (err) {
        // AbortError means the person closed the sheet: nothing to report.
        if (err && err.name !== "AbortError") announce("Sharing didn't work. Try Copy link.");
      }
    });
  }

  const copyText = async (value) => {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return;
    }
    // Older browsers and plain-http local dev: a hidden textarea and execCommand.
    const area = document.createElement("textarea");
    area.value = value;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    if (!ok) throw new Error("copy failed");
  };

  if (copyButton) {
    const label = copyButton.querySelector(".share-copy-label");
    let resetTimer;
    copyButton.hidden = false;
    copyButton.addEventListener("click", async () => {
      try {
        await copyText(url);
        if (label) label.textContent = "Link copied";
        copyButton.classList.add("is-done");
        announce("Link copied to clipboard");
      } catch {
        if (label) label.textContent = "Couldn't copy";
        announce("Couldn't copy the link");
      }
      window.clearTimeout(resetTimer);
      resetTimer = window.setTimeout(() => {
        if (label) label.textContent = "Copy link";
        copyButton.classList.remove("is-done");
      }, 2500);
    });
  }
})();
