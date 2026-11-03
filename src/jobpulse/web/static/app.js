// jobpulse dashboard. Served from here because the Content-Security-Policy allows no inline
// script. After "Block company", the response names the company (HX-Trigger), and its other
// cards on the page go; the one clicked shows the undo.
document.addEventListener("jobpulse:blocked", (event) => {
  const { company, keep } = event.detail;
  for (const card of document.querySelectorAll("article.posting[data-company]")) {
    if (card.dataset.company === company && card.id !== keep) {
      card.remove();
    }
  }
});

// A saved filter's button is a plain link; before it navigates, tell the server the filter was
// opened so its badge of new matches starts over (CSRF token from the page's htmx headers).
document.addEventListener("click", (event) => {
  const link = event.target.closest("a.saved-filter[data-viewed]");
  if (!link) {
    return;
  }
  let token = "";
  try {
    token = JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF-Token"] || "";
  } catch (error) {
    return;
  }
  fetch(link.dataset.viewed, {
    method: "POST",
    headers: { "X-CSRF-Token": token },
    credentials: "same-origin",
    keepalive: true,
  });
});

// The setup wizard fills the timezone from the browser when the field is empty.
document.addEventListener("DOMContentLoaded", () => {
  for (const input of document.querySelectorAll('input[data-autofill="timezone"]')) {
    if (!input.value) {
      try {
        input.value = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
      } catch (error) {
        // leave it empty: the profile keeps its timezone
      }
    }
  }
});

// "Block company" asks first whether the company is a staffing agency (a dialog in
// postings.html), then sends the block with kind=agency or kind=company. Without the dialog
// (an old browser), the block goes through as a company block.
document.addEventListener("htmx:confirm", (event) => {
  const form = event.detail.elt.closest && event.detail.elt.closest("form[data-ask-agency]");
  const dialog = document.getElementById("block-dialog");
  if (!form || !dialog || typeof dialog.showModal !== "function") {
    return;
  }
  event.preventDefault();
  dialog.querySelector("[data-company]").textContent = form.dataset.askAgency;
  dialog.returnValue = "";
  dialog.addEventListener(
    "close",
    () => {
      const choice = dialog.returnValue;
      if (choice === "agency" || choice === "company") {
        form.querySelector('input[name="kind"]').value = choice;
        event.detail.issueRequest(true);
      }
    },
    { once: true },
  );
  dialog.showModal();
});
