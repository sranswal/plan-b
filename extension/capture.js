chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: 'plan-b-deadline', title: 'Review deadline in Plan B', contexts: ['selection'],
    documentUrlPatterns: ['https://mail.google.com/*'],
  });
});

function openCapture(info, subject = '') {
  const draft = new URLSearchParams({email: info.selectionText.slice(0, 12000), source: info.pageUrl.slice(0, 1000)});
  if (subject) draft.set('subject', subject.slice(0, 200));
  chrome.tabs.create({url: `http://127.0.0.1:8080/#${draft}`});
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== 'plan-b-deadline' || !info.selectionText || !info.pageUrl?.startsWith('https://mail.google.com/')) return;
  if (!tab?.id) { openCapture(info); return; }
  chrome.tabs.sendMessage(tab.id, {type: 'plan-b-capture-subject'}, (response) => {
    void chrome.runtime.lastError;
    const subject = typeof response?.subject === 'string' ? response.subject.trim() : '';
    openCapture(info, subject);
  });
});

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || !sender.url?.startsWith('https://mail.google.com/')) return;
  const preview = message.type === 'deadline-preview';
  if (!preview && (message.type !== 'deadline-approve' || !/^[A-Za-z0-9_-]{43}$/.test(message.token))) return;
  const path = preview ? 'preview' : `${message.token}/approve`;
  fetch(`http://127.0.0.1:8080/api/gmail/${path}`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(preview ? {...message.data, source: sender.url.slice(0, 1000)} : {}),
  }).then(async (response) => {
    const data = await response.json();
    reply(response.ok ? data : {error: typeof data.detail === 'string' ? data.detail : 'Plan B could not check this deadline.'});
  }).catch(() => reply({error: 'Start the local Plan B server and reopen the email.'}));
  return true;
});
