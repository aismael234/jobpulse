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
