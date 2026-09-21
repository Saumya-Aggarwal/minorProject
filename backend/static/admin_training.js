// /admin/training: rate and undo without reloading the page.
//
// The forms still work on their own (plain POST + redirect). This sends the
// same POST in the background, then swaps in just the part that changed,
// rendered by the server exactly as a reload would have shown it. If anything
// goes wrong, the form is submitted normally, so a rating is never lost.
(function () {
  const tab = document.getElementById("training-tab");
  if (!tab) return;

  function flash(text) {
    const note = document.createElement("span");
    note.textContent = text;
    note.className = "pointer-events-none fixed bottom-6 left-1/2 z-50 -translate-x-1/2 bg-ink px-4 py-2 text-[13px] text-ivory shadow-lg";
    document.body.appendChild(note);
    setTimeout(() => note.remove(), 1600);
  }

  tab.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    event.preventDefault();
    const submitter = event.submitter;
    // Read the form BEFORE disabling: a disabled button's value (rating=1) is not sent
    const body = new FormData(form, submitter);
    const buttons = form.querySelectorAll("button");
    buttons.forEach((b) => (b.disabled = true));
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body,
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      // fetch followed the redirect: this is the page as a reload would show it
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const card = form.closest("li[id^='reply-']");
      const fresh = card && page.getElementById(card.id);
      if (fresh) {
        card.replaceWith(fresh);
      } else {
        const freshTab = page.getElementById("training-tab");
        if (!freshTab) throw new Error("unexpected page");
        tab.innerHTML = freshTab.innerHTML;
      }
      flash(form.action.includes("/undo/") ? "Undone" : "Saved");
    } catch (error) {
      console.warn("background save failed, submitting normally:", error);
      buttons.forEach((b) => (b.disabled = false));
      if (submitter && submitter.name) {
        // keep which button was pressed (rating=1 / rating=-1)
        const hidden = Object.assign(document.createElement("input"),
          { type: "hidden", name: submitter.name, value: submitter.value });
        form.appendChild(hidden);
      }
      form.submit();
    }
  });
})();
