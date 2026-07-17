document.querySelectorAll('form').forEach((form) => {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button[type="submit"]');
    if (button) {
      button.disabled = true;
      button.dataset.label = button.textContent;
      button.textContent = '处理中…';
    }
  });
});

const notices = document.querySelectorAll('.notice');
if (notices.length) {
  window.setTimeout(() => notices.forEach((notice) => notice.classList.add('fade')), 4500);
}

if (document.querySelector('.job-status.running')) {
  window.setTimeout(() => window.location.reload(), 5000);
}
