const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const selection = 'Submission deadline: November 25, 2026 at 11:59 PM IST';
const pageUrl = 'https://mail.google.com/mail/u/0/#inbox/message';

function captureUrl(subjectResponse) {
  let clicked;
  let opened;
  const chrome = {
    contextMenus: {
      create() {},
      onClicked: {addListener(listener) { clicked = listener; }},
    },
    runtime: {
      lastError: undefined,
      onInstalled: {addListener() {}},
      onMessage: {addListener() {}},
    },
    tabs: {
      create({url}) { opened = url; },
      sendMessage(_tabId, _message, reply) { reply(subjectResponse); },
    },
  };
  vm.runInNewContext(fs.readFileSync(__dirname + '/capture.js', 'utf8'), {chrome, URLSearchParams});
  clicked({menuItemId: 'plan-b-deadline', selectionText: selection, pageUrl}, {id: 12});
  return new URL(opened);
}

test('current Gmail subject is included without expanding the selected body text', () => {
  const subject = 'Vibe-a-thon Problem Statements and Submission Deadline';
  const url = captureUrl({subject});
  const draft = new URLSearchParams(url.hash.slice(1));
  assert.equal(draft.get('subject'), subject);
  assert.equal(draft.get('email'), selection);
});

test('missing Gmail subject preserves the existing fallback payload', () => {
  const url = captureUrl(undefined);
  const draft = new URLSearchParams(url.hash.slice(1));
  assert.equal(draft.has('subject'), false);
  assert.equal(draft.get('email'), selection);
});

test('content script returns only the currently opened message subject', () => {
  let receive;
  const subject = 'Vibe-a-thon Problem Statements and Submission Deadline';
  const main = {querySelector(selector) { return selector === 'h2.hP' ? {textContent: ` ${subject} `} : null; }};
  const context = {
    chrome: {runtime: {onMessage: {addListener(listener) { receive = listener; }}}},
    document: {documentElement: {}, querySelector(selector) { return selector === '[role="main"]' ? main : null; }},
    MutationObserver: class { observe() {} },
    addEventListener() {},
    clearTimeout() {},
    setTimeout() { return 1; },
  };
  vm.runInNewContext(fs.readFileSync(__dirname + '/gmail.js', 'utf8'), context);
  let response;
  receive({type: 'plan-b-capture-subject'}, {}, value => { response = value; });
  assert.deepEqual({...response}, {subject});
});
