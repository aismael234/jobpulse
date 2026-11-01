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
