const setFormProcessing = (form, processing) => {
  const button = form.querySelector('button[type="submit"]');
  if (button) {
    button.disabled = processing;
    if (!button.hasAttribute('data-icon-button')) {
      if (!button.dataset.label) button.dataset.label = button.textContent;
      button.textContent = processing ? '处理中…' : button.dataset.label;
    }
  }

  if (processing && form.hasAttribute('data-background-job')) {
    const overlay = document.querySelector('[data-job-overlay]');
    const message = overlay?.querySelector('[data-job-message]');
    if (message && form.dataset.jobLabel) message.textContent = form.dataset.jobLabel;
    if (overlay) overlay.hidden = false;
  }
};

document.querySelectorAll('form').forEach((form) => {
  form.addEventListener('submit', async (event) => {
    if (!form.hasAttribute('data-stock-validation')) {
      setFormProcessing(form, true);
      return;
    }
    if (form.dataset.validating === '1') {
      event.preventDefault();
      return;
    }

    event.preventDefault();
    const codeInput = form.querySelector('input[name="code"]');
    if (!codeInput) return;
    form.dataset.validating = '1';
    setFormProcessing(form, true);
    try {
      const response = await fetch('/api/securities/resolve', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({code: codeInput.value}),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) {
        throw new Error(result.message || '未能确认该A股代码。');
      }
      codeInput.value = result.code;
      form.submit();
    } catch (error) {
      window.alert(error.message || 'A股代码查询失败，禁止录入。');
      form.dataset.validating = '0';
      setFormProcessing(form, false);
      codeInput.focus();
      codeInput.select();
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

document.querySelectorAll('[data-stock-row]').forEach((row) => {
  const openStock = () => {
    if (row.dataset.href) window.location.assign(row.dataset.href);
  };
  row.addEventListener('click', (event) => {
    if (!event.target.closest('a, button, input, select')) openStock();
  });
  row.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') openStock();
  });
});
