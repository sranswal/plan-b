const find = (selector) => document.querySelector(selector);
const question = find('#question');
const run = find('#run');
const answer = find('#answer');
const proposalPanel = find('#proposal');
const evidence = find('#evidence');
const sourceStatus = find('#source-status');
const calendarNote = find('#calendar-note');
const refresh = find('#refresh');
const followUpForm = find('#follow-up');
const followUpReply = find('#follow-up-reply');
const changeTime = find('#change-time');
const reviewSlot = find('#review-slot');
const examples = {
  focus: 'Find 90 minutes to finish my hackathon README before its deadline.',
  routine: 'Can I fit three gym sessions into this week while protecting my meetings and hackathon work?',
};
const weekDays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
const dateKey = new Intl.DateTimeFormat('sv-SE', {year: 'numeric', month: '2-digit', day: '2-digit'});
const clock = new Intl.DateTimeFormat('en-US', {hour: 'numeric', minute: '2-digit', hour12: true});
const calendarDate = new Intl.DateTimeFormat('en-GB', {weekday: 'short', day: 'numeric', month: 'short'});
let actionToken = '';
let mode = 'auto';
let lastCalls = [];
let listedTools = new Set();
let currentBlocks = [];
let provider = 'fixture';
let tasksProvider = 'fixture';
let futureBlocks = [];
let calendarBlocks = [];
let dashboardTasks = [];
let firstDay = '2026-11-23';
let emailSource = '';
let followUpTo = '';
let timeRevision = '';
let revisingTime = false;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-Plan-B-Action': actionToken}, body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Plan B request failed (${response.status}).`);
  return data;
}

const blockDate = (block) => block.dateOnly ? block.start : dateKey.format(new Date(block.start));
const taskBlocks = () => dashboardTasks.filter((task) => task.deadline).map((task) => ({
  kind: 'deadline', title: task.title, start: task.deadline, dateOnly: task.deadline.length === 10,
}));

async function loadTasks() {
  try {
    renderTasks(await api('/api/tasks'));
  } catch (error) {
    find('#dashboard-tasks').textContent = error.message;
  }
}

function renderTasks(tasks) {
  dashboardTasks = tasks;
  const list = find('#dashboard-tasks');
  list.replaceChildren();
  for (const task of tasks) {
    const form = node('form', undefined, 'task-row');
    const due = task.deadline;
    const deadline = due ? (due.length === 10 ? calendarDate.format(new Date(due + 'T12:00:00'))
      : `${calendarDate.format(new Date(due))} at ${clock.format(new Date(due))}`) : 'No deadline';
    const estimate = node('input');
    estimate.type = 'number';
    estimate.min = '1';
    estimate.max = '1440';
    estimate.placeholder = 'Minutes';
    estimate.value = task.estimated_minutes ?? '';
    estimate.setAttribute('aria-label', `Estimated minutes for ${task.title}`);
    const save = node('button', 'Save estimate');
    const status = node('small');
    form.append(node('strong', task.title), node('span', deadline), estimate, save, status);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      save.disabled = true;
      try {
        renderTasks(await api('/api/tasks/effort', {task_id: task.id, minutes: estimate.value ? Number(estimate.value) : null}));
        proposalPanel.replaceChildren();
        calendarNote.textContent = 'Estimate updated. Ask for a fresh plan to use it.';
      } catch (error) {
        status.textContent = error.message;
        save.disabled = false;
      }
    });
    list.append(form);
  }
  if (!tasks.length) list.textContent = 'No pending tasks.';
  renderWeek(currentBlocks);
  showUpcoming(futureBlocks);
}

function renderWeek(blocks) {
  currentBlocks = blocks;
  const week = find('#week');
  week.replaceChildren();
  for (const index of weekDays.keys()) {
    const date = new Date(`${firstDay}T00:00:00Z`);
    date.setUTCDate(date.getUTCDate() + index);
    const key = date.toISOString().slice(0, 10);
    const name = weekDays[(date.getUTCDay() + 6) % 7];
    const day = node('div', undefined, 'day');
    const heading = node('div', name, 'day-head');
    heading.append(node('b', date.getUTCDate()));
    day.append(heading);
    const items = [...blocks, ...taskBlocks()].filter((block) => {
      if (block.kind === 'fixed') {
        const last = dateKey.format(new Date(Date.parse(block.end) - 1));
        return blockDate(block) <= key && last >= key;
      }
      return blockDate(block) === key;
    })
      .sort((first, second) => Date.parse(first.start) - Date.parse(second.start));
    for (const item of items) {
      const block = node('div', undefined, `block ${item.kind}`);
      const when = item.kind === 'deadline' ? (item.dateOnly ? 'Task due' : `Due ${clock.format(new Date(item.start))}`)
        : `${clock.format(new Date(item.start))}–${clock.format(new Date(item.end))}`;
      block.append(node('time', when), node('strong', item.title));
      day.append(block);
    }
    week.append(day);
  }
}

function showUpcoming(blocks) {
  const container = find('#upcoming');
  container.replaceChildren();
  for (const offset of [7, 14]) {
    const start = new Date(`${firstDay}T00:00:00Z`);
    start.setUTCDate(start.getUTCDate() + offset);
    const end = new Date(start);
    end.setUTCDate(end.getUTCDate() + 7);
    const entries = [...blocks, ...taskBlocks()].filter((block) => blockDate(block) >= start.toISOString().slice(0,10) &&
      blockDate(block) < end.toISOString().slice(0,10));
    const section = node('details');
    const label = new Intl.DateTimeFormat('en-GB', {month: 'short', day: 'numeric', timeZone: 'UTC'}).format(start);
    const deadlines = entries.filter((entry) => entry.kind === 'deadline').length;
    section.open = deadlines > 0;
    section.append(node('summary', `Week of ${label} · ${entries.length - deadlines} events · ${deadlines} tasks`));
    for (const block of entries) {
      const date = new Date(block.dateOnly ? block.start + 'T12:00:00' : block.start);
      const time = block.dateOnly ? 'Task due' : (block.kind === 'deadline' ? 'Due ' : '') + clock.format(date);
      section.append(node('p', `${calendarDate.format(date)} · ${time} · ${block.title}`));
    }
    container.append(section);
  }
  const end = new Date(firstDay + 'T00:00:00Z');
  end.setUTCDate(end.getUTCDate() + 21);
  const outside = dashboardTasks.filter((task) => {
    if (!task.deadline) return true;
    const key = blockDate({start: task.deadline, dateOnly: task.deadline.length === 10});
    return key < firstDay || key >= end.toISOString().slice(0, 10);
  });
  if (outside.length) {
    const section = node('details');
    section.open = true;
    section.append(node('summary', 'Other pending tasks'));
    for (const task of outside) {
      const due = task.deadline;
      const date = due ? new Date(due.length === 10 ? due + 'T12:00:00' : due) : null;
      section.append(node('p', date ? `${calendarDate.format(date)} ${date.getFullYear()} · ${task.title}` : `No date · ${task.title}`));
    }
    container.append(section);
  }
}

function renderSources(sources, highlight = false) {
  const container = find('#catalog');
  container.replaceChildren();
  const next = new Set();
  const added = [];
  for (const source of sources) {
    const body = node('div', undefined, 'source');
    body.append(node('strong', `${source.name === 'context' ? 'Context' : 'Tasks & routines'} MCP`), node('small', source.file));
    for (const tool of source.tools) {
      const qualified = `${source.name}/${tool.name}`;
      next.add(qualified);
      const isNew = highlight && !listedTools.has(qualified);
      if (isNew) added.push(qualified);
      const badge = node('code', tool.name, isNew ? 'new' : undefined);
      if (tool.description) badge.title = tool.description;
      body.append(badge);
    }
    container.append(body);
  }
  listedTools = next;
  find('#source-summary').textContent = `${sources.length} servers · ${next.size} tools`;
  return added;
}

function renderCalls(calls) {
  find('#calls').replaceChildren(...(calls.length ? calls.map((call) => node('li', `${call.server}/${call.tool} · ${call.status}`)) : [node('li', 'No calls in this run.')]))
}

function setMode(next) {
  mode = next;
  find('#email-fields').hidden = mode !== 'email';
  find('#voice').hidden = mode === 'email';
  question.placeholder = mode === 'email' ? 'Selected email text' : 'Ask to find a slot, book time, or add a task…';
  question.readOnly = mode === 'email';
}

function showProposal(proposal) {
  proposalPanel.replaceChildren();
  proposalPanel.hidden = !proposal;
  if (!proposal) return;
  const task = proposal.action === 'task';
  const due = task ? proposal.deadline : proposal.starts_at;
  const dateOnly = due && /^\d{4}-\d{2}-\d{2}$/.test(due);
  const start = due ? new Date(dateOnly ? `${due}T12:00:00` : due) : null;
  const end = task ? null : new Date(proposal.ends_at);
  const when = start ? `${calendarDate.format(start)}${dateOnly ? '' : `, ${clock.format(start)}${end ? `–${clock.format(end)}` : ''}`}` : 'No due date';
  const target = task ? (tasksProvider === 'google' ? 'Google Tasks' : 'Plan B Tasks') : provider === 'google' ? 'Google Calendar' : 'fixture calendar';
  const approve = node('button', 'Approve & save');
  const decline = node('button', 'Decline');
  proposalPanel.replaceChildren(node('strong', `Add to ${target}`), node('div', proposal.title), node('div', when));
  if (task && tasksProvider === 'google' && due && !dateOnly) proposalPanel.append(node('div', 'Google Tasks stores the date. The exact time goes in the notes.'));
  if (proposal.source_quote) proposalPanel.append(node('blockquote', proposal.source_quote));
  if (proposal.source_url) {
    const link = node('a', 'Open source email');
    link.href = proposal.source_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    proposalPanel.append(link);
  }
  proposalPanel.append(approve, decline);
  approve.focus();
  for (const [button, decision] of [[approve, 'approve'], [decline, 'decline']]) {
    button.type = 'button';
    button.addEventListener('click', async () => {
      approve.disabled = decline.disabled = run.disabled = true;
      const status = node('div', decision === 'approve' ? 'Rechecking both systems…' : 'Declining…');
      proposalPanel.append(status);
      try {
        const result = await api(`/api/proposals/${proposal.id}/${decision}`, {});
        renderCalls([...lastCalls, ...result.calls]);
        const message = {verified: task ? `Added ${proposal.title}` : `Booked ${proposal.title} · ${when}`,
                         declined: 'Declined. No changes saved.',
                         unverified: `Couldn’t confirm whether ${proposal.title} was saved. Check ${target} before trying again.`}[result.status];
        proposalPanel.replaceChildren(node('strong', message));
        if (result.status === 'verified') {
          answer.hidden = true;
          changeTime.hidden = followUpForm.hidden = true;
        }
        if (result.status === 'verified' && task) await loadTasks();
        if (result.status === 'verified' && !task) {
          calendarBlocks.push({title: proposal.title, start: proposal.starts_at, end: proposal.ends_at, kind: 'fixed'});
          renderWeek(currentBlocks.map((block) => block.kind !== 'fixed' && block.title === proposal.title && block.start === proposal.starts_at
            ? {...block, kind: 'fixed'} : block));
          calendarNote.textContent = `The approved block is now saved to ${target}.`;
        }
      } catch (error) {
        proposalPanel.replaceChildren(node('div', error.message));
      } finally {
        run.disabled = false;
      }
    });
  }
}

find('#open-sources').addEventListener('click', () => {
  evidence.open = true;
  evidence.scrollIntoView({behavior: 'smooth', block: 'start'});
  evidence.querySelector('summary').focus({preventScroll: true});
});
document.querySelectorAll('[data-example]').forEach((button) => button.addEventListener('click', () => {
  followUpTo = '';
  changeTime.hidden = true;
  reviewSlot.hidden = true;
  followUpForm.hidden = true;
  setMode('auto');
  question.value = examples[button.dataset.example];
  question.focus();
}));

async function sendRequest(text, isReply = false) {
  if (!actionToken || !text.trim() || run.disabled) return;
  run.disabled = true;
  const replyButton = followUpForm.querySelector('[type="submit"]');
  replyButton.disabled = true;
  answer.hidden = false;
  answer.textContent = 'Checking both Synapse servers…';
  reviewSlot.hidden = true;
  showProposal(null);
  renderCalls([]);
  try {
    const body = {mode, question: text.trim(), timezone};
    if (isReply && followUpTo) {
      body.follow_up_to = followUpTo;
      body.revise_time = revisingTime;
    }
    if (mode === 'email') {
      body.subject = find('#email-subject').value.trim();
      body.source_url = emailSource;
      const reference = find('#email-reference').value;
      if (reference) body.reference = new Date(reference).toISOString();
      body.email_destination = find('#email-destination').value;
    }
    const result = await api('/api/plan', body);
    answer.textContent = result.answer;
    followUpTo = result.follow_up || '';
    timeRevision = result.time_revision || '';
    revisingTime = false;
    changeTime.hidden = !timeRevision;
    reviewSlot.hidden = !timeRevision || !!result.proposal || !result.blocks?.some((block) => block.kind === 'focus');
    followUpForm.hidden = !followUpTo;
    followUpReply.placeholder = 'Your reply…';
    if (followUpTo) {
      followUpReply.value = '';
      find('#follow-up-voice-status').hidden = true;
      followUpReply.focus();
    }
    if (result.evidence) answer.append(node('blockquote', result.evidence));
    lastCalls = result.calls;
    renderCalls(lastCalls);
    if (result.blocks) {
      const merged = new Map();
      for (const block of [...calendarBlocks, ...result.blocks]) {
        merged.set(JSON.stringify([block.kind, block.title, block.start, block.end]), block);
      }
      const blocks = [...merged.values()];
      renderWeek(blocks);
      futureBlocks = blocks;
      showUpcoming(blocks);
    }
    if (result.router) sourceStatus.textContent = `${result.router} identified the request. Synapse supplied the tool results.`;
    showProposal(result.proposal);
  } catch (error) {
    answer.textContent = error.message;
  } finally {
    run.disabled = false;
    replyButton.disabled = false;
  }
}

find('#planner').addEventListener('submit', (event) => {
  event.preventDefault();
  sendRequest(question.value);
});
followUpForm.addEventListener('submit', (event) => {
  event.preventDefault();
  sendRequest(followUpReply.value, true);
});
reviewSlot.addEventListener('click', () => sendRequest('Book it'));
changeTime.addEventListener('click', () => {
  followUpTo = timeRevision;
  revisingTime = true;
  followUpForm.hidden = false;
  followUpReply.value = '';
  followUpReply.placeholder = 'Try lunch, late morning, or Sunday at 6 pm';
  followUpReply.focus();
});

refresh.addEventListener('click', async () => {
  refresh.disabled = true;
  sourceStatus.textContent = 'Reconnecting to the latest generated Tasks server…';
  try {
    const result = await api('/api/refresh', {});
    const added = renderSources(result.sources, true);
    sourceStatus.textContent = added.length ? `Newly discovered: ${added.join(', ')}. Run the same question again.` : 'Catalog reloaded. No new tools were found.';
  } catch (error) {
    sourceStatus.textContent = error.message;
  } finally {
    refresh.disabled = false;
  }
});

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition;
function bindDictation(voice, voiceStatus, field) {
  voice.addEventListener('click', () => {
    voiceStatus.hidden = false;
    if (!Recognition) {
      voiceStatus.textContent = 'Dictation is unavailable in this browser. You can type your request.';
      return;
    }
    if (recognition) { recognition.stop(); return; }
    const current = new Recognition();
    recognition = current;
    current.lang = 'en-IN';
    current.continuous = true;
    current.interimResults = true;
    const existingText = field.value.trim();
    let transcript = '';
    let failed = false;
    let silenceTimer;
    const resetSilence = () => {
      clearTimeout(silenceTimer);
      silenceTimer = setTimeout(() => {
        if (recognition === current) current.stop();
      }, 1500);
    };
    current.onstart = () => {
      voice.setAttribute('aria-pressed', 'true');
      voice.textContent = 'Stop dictation';
      voiceStatus.textContent = 'Listening…';
      resetSilence();
    };
    current.onspeechstart = () => clearTimeout(silenceTimer);
    current.onspeechend = resetSilence;
    current.onresult = (event) => {
      transcript = Array.from(event.results, result => result[0].transcript.trim()).filter(Boolean).join(' ');
      field.value = [existingText, transcript].filter(Boolean).join(' ');
      resetSilence();
    };
    current.onerror = () => {
      failed = true;
      voiceStatus.textContent = transcript
        ? 'Dictation stopped. Your transcript is still here. You can dictate again or type.'
        : 'Dictation stopped without a transcript. You can try again or type.';
    };
    current.onend = () => {
      clearTimeout(silenceTimer);
      recognition = undefined;
      voice.setAttribute('aria-pressed', 'false');
      voice.textContent = '🎙 Dictate';
      if (!failed) voiceStatus.textContent = transcript
        ? 'Review the transcript, or tap Dictate to continue.'
        : 'No speech was transcribed. You can try again or type.';
    };
    try { current.start(); } catch {
      recognition = undefined;
      voiceStatus.textContent = 'Could not start dictation.';
    }
  });
}
function dismissReply() {
  followUpTo = '';
  revisingTime = false;
  followUpForm.hidden = true;
  changeTime.hidden = true;
  reviewSlot.hidden = true;
}
question.addEventListener('input', dismissReply);
find('#voice').addEventListener('click', dismissReply);
bindDictation(find('#voice'), find('#voice-status'), question);
bindDictation(find('#follow-up-voice'), find('#follow-up-voice-status'), followUpReply);

const capture = new URLSearchParams(location.hash.slice(1));
if (capture.has('email') && capture.get('source')?.startsWith('https://mail.google.com/')) {
  history.replaceState(null, '', location.pathname + location.search);
  setMode('email');
  question.value = capture.get('email').slice(0, 12000);
  find('#email-subject').value = (capture.get('subject') || '').trim().slice(0, 200);
  emailSource = (capture.get('source') || '').slice(0, 1000);
}
renderWeek([]);
run.disabled = refresh.disabled = true;
api(`/api/catalog?timezone=${encodeURIComponent(timezone)}`).then((data) => {
  actionToken = data.actionToken;
  provider = data.provider;
  tasksProvider = data.tasksProvider;
  firstDay = data.date;
  const endDate = new Date(`${firstDay}T12:00:00`);
  const startDate = new Date(endDate);
  endDate.setUTCDate(endDate.getUTCDate() + 6);
  find('#week-range').textContent = `${calendarDate.format(startDate)} – ${calendarDate.format(endDate)}`;
  find('#provider').textContent = provider === 'google' ? 'GOOGLE CALENDAR' : 'FIXTURE CALENDAR';
  find('#tasks-provider').textContent = tasksProvider === 'google' ? 'Google Tasks connected' : 'Local Tasks';
  renderSources(data.sources);
  calendarBlocks = data.blocks;
  renderWeek(data.blocks);
  futureBlocks = data.blocks;
  showUpcoming(futureBlocks);
  loadTasks();
  calendarNote.textContent = data.blocks.length
    ? `${data.blocks.length} calendar commitments loaded across the displayed weeks.`
    : 'No calendar events in the displayed weeks from the connected calendar.';
  run.disabled = refresh.disabled = false;
}).catch((error) => {
  calendarNote.textContent = error.message;
});
