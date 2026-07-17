document.querySelectorAll('form').forEach((form) => {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button[type="submit"]');
    if (button) {
      button.disabled = true;
      button.dataset.label = button.textContent;
      button.textContent = '处理中…';
    }

    if (form.hasAttribute('data-background-job')) {
      const overlay = document.querySelector('[data-job-overlay]');
      const message = overlay?.querySelector('[data-job-message]');
      if (message && form.dataset.jobLabel) message.textContent = form.dataset.jobLabel;
      if (overlay) overlay.hidden = false;
    }
  });
});

const notices = document.querySelectorAll('.notice');
if (notices.length) {
  window.setTimeout(() => notices.forEach((notice) => notice.classList.add('fade')), 4500);
}

if (document.querySelector('.job-status.running')) {
  window.setTimeout(() => window.location.reload(), 3000);
}
