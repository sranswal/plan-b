(() => {
  let currentMessage = '';
  let card;
  let timer;

  function currentSubject() {
    return (document.querySelector('[role="main"]')?.querySelector('h2.hP')?.textContent.trim() || '').slice(0, 200);
  }

  chrome.runtime.onMessage.addListener((message, _, reply) => {
    if (message?.type !== 'plan-b-capture-subject') return;
    reply({subject: currentSubject()});
  });

  function show(body, result) {
    card?.remove();
    card = document.createElement('div');
    const shadow = card.attachShadow({mode: 'closed'});
    const style = document.createElement('style');
    style.textContent = `
      section{font:14px Arial,sans-serif;color:#eaf0e3;background:#253325;border-radius:10px;padding:14px;margin:8px 0 16px}
      p,blockquote{margin:7px 0}blockquote{font-size:13px;color:#d0dbc8}
      button{font:inherit;margin:7px 8px 0 0;padding:7px 11px;border:1px solid #78916a;border-radius:6px;cursor:pointer}
      button:first-of-type{background:#d9eb9c;color:#26311e}button:disabled{opacity:.6}
    `;
    const panel = document.createElement('section');
    const title = document.createElement('strong');
    title.textContent = 'Plan B · Deadline found';
    panel.append(title);
    if (result.error) {
      title.textContent = 'Plan B';
      const status = document.createElement('p');
      status.textContent = result.error;
      panel.append(status);
    } else {
      const detail = document.createElement('p');
      const time = new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short'}).format(new Date(result.deadline));
      detail.textContent = `${result.title} · ${time}`;
      const evidence = document.createElement('blockquote');
      evidence.textContent = result.quote;
      const add = document.createElement('button');
      add.type = 'button';
      add.textContent = 'Add to Calendar';
      const dismiss = document.createElement('button');
      dismiss.type = 'button';
      dismiss.textContent = 'Dismiss';
      const status = document.createElement('p');
      status.setAttribute('role', 'status');
      const host = card;
      dismiss.addEventListener('click', (event) => { if (event.isTrusted) host.remove(); });
      add.addEventListener('click', async (event) => {
        if (!event.isTrusted) return;
        add.disabled = true;
        status.textContent = 'Adding deadline…';
        try {
          const saved = await chrome.runtime.sendMessage({type: 'deadline-approve', token: result.token});
          status.textContent = saved?.status === 'verified' ? 'Added to Google Calendar.'
            : saved?.error || 'Couldn’t verify the save. Check Google Calendar before trying again.';
        } catch {
          status.textContent = 'Couldn’t verify the save. Check Google Calendar before trying again.';
        }
      });
      panel.append(detail, evidence, add, dismiss, status);
    }
    shadow.append(style, panel);
    body.before(card);
  }

  async function inspectMessage() {
    const main = document.querySelector('[role="main"]');
    const bodies = [...(main?.querySelectorAll('.a3s') || [])].filter((body) => body.getClientRects().length);
    const body = bodies.at(-1);
    const subject = currentSubject();
    const copy = body?.cloneNode(true);
    copy?.querySelectorAll('.gmail_quote, blockquote, .gmail_signature').forEach((part) => part.remove());
    copy?.querySelectorAll('br').forEach((part) => part.replaceWith('\n'));
    const message = copy?.textContent.trim().slice(0, 12000) || '';
    const key = body ? JSON.stringify([location.href, body.id, subject, message]) : '';
    if (key === currentMessage) return;
    currentMessage = key;
    card?.remove();
    if (!body || !/\b(?:deadline|due(?:\s+on)?|by)\s*:?[ \t]+(?:\d{4}-\d{2}-\d{2}|(?:january|february|march|april|may|june|july|august|september|october|november|december)[ \t]+\d{1,2},?[ \t]+\d{4}|today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b/i.test(message)) return;
    const stamp = body.closest('.adn')?.querySelector('.g3[title]')?.getAttribute('title');
    const date = stamp ? new Date(stamp) : null;
    try {
      const result = await chrome.runtime.sendMessage({type: 'deadline-preview', data: {
        message, subject: subject.slice(0, 200),
        reference: date && !Number.isNaN(date.getTime()) ? date.toISOString() : '',
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      }});
      if (key === currentMessage && body.isConnected && (result?.token || result?.error)) show(body, result);
    } catch {
      if (key === currentMessage && body.isConnected) show(body, {error: 'Reload Gmail to reconnect Plan B.'});
    }
  }

  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(inspectMessage, 350);
  };
  new MutationObserver(schedule).observe(document.documentElement, {childList: true, subtree: true});
  addEventListener('hashchange', schedule);
  schedule();
})();
