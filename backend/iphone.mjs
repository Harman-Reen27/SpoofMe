// Keep the Node/Python boundary here so Electron only deals with ordinary methods. One child
// process owns the phone session. Requests and responses are JSON lines; the adapter matches their IDs and stops
// on communication failures rather than replaying Set.

import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Resolve paths from this module so the app does not depend on the terminal's folder.
const root = fileURLToPath(new URL('../', import.meta.url));
const defaultPython = fileURLToPath(new URL('../.venv/bin/python', import.meta.url));
const defaultBridge = fileURLToPath(new URL('../iphone/bridge.py', import.meta.url));

class PhoneError extends Error {
  constructor(code, message) {
    // Preserve a short code alongside the display message from the bridge.
    super(message);
    this.code = code;
  }
}

export class IPhone {
  constructor({ python = defaultPython, bridge = defaultBridge, timeout = 210_000,
                stopTimeout = 75_000 } = {}) {
    // Allow one pending request, keeping Set, Clear and shutdown from competing.
    this.python = python;
    this.bridge = bridge;
    this.timeout = timeout;
    this.stopTimeout = stopTimeout;
    this.nextId = 1;
    this.device = null;
    this.child = null;
    this.pending = null;
    this.buffer = '';
    this.failed = false;
    this.closing = null;
    this.termination = null;
  }

  async start() {
    if (this.child) throw new PhoneError('ALREADY_STARTED', 'Create a new adapter to start another bridge.');

    // Pass Python and its arguments separately. No shell interprets coordinates or device data.
    this.child = spawn(this.python, ['-u', this.bridge], {
      cwd: root, shell: false, stdio: ['pipe', 'pipe', 'pipe'],
    });
    // Track actual process exit separately from the reply to a shutdown request.
    this.closed = new Promise((resolve) => { this.finish = resolve; });
    this.child.stdout.setEncoding('utf8');
    this.child.stdout.on('data', (chunk) => this.receive(chunk));

    // Discard raw Python diagnostics because they can contain device IDs or credentials.
    this.child.stderr.resume();
    this.child.stdin.on('error', () => this.fail('BRIDGE_IO', 'The Python input pipe closed.'));
    this.child.stdout.on('error', () => this.fail('BRIDGE_IO', 'The Python output pipe closed.'));
    this.child.on('error', () => {
      this.fail('BRIDGE_START', 'Could not start Python. Check the project virtual environment.');
    });
    this.child.on('close', () => {
      clearTimeout(this.termination);
      this.rejectPending(new PhoneError('BRIDGE_EXITED',
        'The Python bridge exited. Any active location is unverified; reconnect and run Clear.'));
      this.failed = true;
      this.device = null;
      this.finish();
    });

    // Wait for Status before treating Python startup and its recovery lock as successful.
    return await this.request('status');
  }

  receive(chunk) {
    // Buffer pipe chunks until a newline arrives; a chunk is not necessarily one response.
    this.buffer += chunk;
    if (this.buffer.length > 65_536) {
      this.fail('PROTOCOL_ERROR', 'The Python response was too large.');
      return;
    }
    let newline;
    while ((newline = this.buffer.indexOf('\n')) !== -1) {
      const line = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + 1);
      let message;
      try { message = JSON.parse(line); }
      catch {
        this.fail('PROTOCOL_ERROR', 'Python returned an invalid response.');
        return;
      }

      // Check the response shape and ID before letting it complete the active request.
      if (!message || typeof message !== 'object' || typeof message.ok !== 'boolean') {
        this.fail('PROTOCOL_ERROR', 'Python returned an unexpected response.');
        return;
      }
      // A null ID belongs to startup or malformed input, never to a normal pending call.
      if (message.id === null && message.ok === false) {
        this.fail('BRIDGE_START', 'The Python bridge could not initialise. Check dependencies and other running commands.');
        return;
      }
      if (!this.pending || message.id !== this.pending.id) {
        if (!this.failed) this.fail('PROTOCOL_ERROR', 'Python returned a mismatched request ID.');
        return;
      }
      // Validate first so the common failure handler can still reject the waiting promise.
      const valid = message.ok
        ? message.result && typeof message.result === 'object' && !Array.isArray(message.result)
        : message.error && typeof message.error.code === 'string' && typeof message.error.message === 'string';
      if (!valid) {
        this.fail('PROTOCOL_ERROR', 'Python returned an invalid response.');
        return;
      }
      // Remove the timer before settling the promise, including error responses.
      const pending = this.pending;
      this.pending = null;
      clearTimeout(pending.timer);
      if (message.ok) pending.resolve(message.result);
      else pending.reject(new PhoneError(message.error.code, message.error.message));
    }
  }

  request(command, fields = {}) {
    if (!this.child || this.failed || this.closing) {
      return Promise.reject(new PhoneError('NOT_RUNNING', 'The Python bridge is not available.'));
    }
    if (this.pending) return Promise.reject(new PhoneError('BUSY', 'Wait for the current phone command to finish.'));

    // Give every request a new ID and encode it before starting its timeout.
    const id = this.nextId++;
    let line;
    try { line = JSON.stringify({ ...fields, id, command }) + '\n'; }
    catch { return Promise.reject(new PhoneError('INVALID_REQUEST', 'The command cannot be encoded as JSON.')); }
    if (Buffer.byteLength(line, 'utf8') > 16_384) {
      return Promise.reject(new PhoneError('INVALID_REQUEST', 'The command is too large.'));
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        // Stop a timed-out bridge instead of retrying Set, which may already have reached the
        // phone.
        this.fail('TIMEOUT', 'The command timed out and may have reached the phone. Check its location before continuing.');
      }, this.timeout);
      this.pending = { id, timer, resolve, reject };
      this.child.stdin.write(line, (error) => {
        if (error) this.fail('BRIDGE_IO', 'Could not send the command to Python.');
      });
    });
  }

  rejectPending(error) {
    // Reject the waiting promise and remove its timer when communication fails.
    if (!this.pending) return;
    clearTimeout(this.pending.timer);
    this.pending.reject(error);
    this.pending = null;
  }

  fail(code, message) {
    // An unreliable pipe rejects the pending request and stops the bridge.
    this.failed = true;
    this.rejectPending(new PhoneError(code, message));
    this.terminate();
  }

  terminate() {
    // Send SIGTERM first so Python gets a chance to Clear before a forced stop.
    if (!this.child || this.child.exitCode !== null || this.child.signalCode !== null || this.termination) return;
    this.child.kill('SIGTERM');
    this.termination = setTimeout(() => this.child.kill('SIGKILL'), this.stopTimeout);
    // The fallback kill timer must not keep Node alive after everything else has closed.
    this.termination.unref();
  }

  // Keep the prepared handle here and attach it to every location-changing request.
  discover() { return this.request('discover'); }
  status() { return this.request('status'); }
  async prepare(device) {
    if (typeof device !== 'string' || !device) throw new PhoneError('INVALID_DEVICE', 'Choose a listed device.');
    if (this.pending) throw new PhoneError('BUSY', 'Wait for the current phone command to finish.');
    // Bind the handle only after setup succeeds; a failed switch must not reuse the old target.
    this.device = null;
    const result = await this.request('prepare', { device });
    this.device = device;
    return result;
  }
  setLocation(latitude, longitude) {
    if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90 ||
        !Number.isFinite(longitude) || longitude < -180 || longitude > 180) {
      return Promise.reject(new PhoneError('INVALID_REQUEST', 'Enter valid latitude and longitude values.'));
    }
    return this.request('set', { device: this.device, latitude, longitude });
  }
  clearLocation() { return this.request('clear', { device: this.device }); }
  confirmRestored(confirmed) {
    if (confirmed !== true) return Promise.reject(new PhoneError('INVALID_REQUEST', 'Check Apple Maps before confirming.'));
    return this.request('confirm', { device: this.device, confirmed });
  }

  shutdown() {
    if (this.closing) return this.closing;
    if (!this.child) return Promise.resolve();

    // Request normal shutdown when idle, or interrupt a busy bridge so Python can clean up.
    const stop = async () => {
      try {
        if (this.failed || this.pending) {
          this.terminate();
          await this.closed;
          throw new PhoneError('STATE_UNKNOWN', 'The bridge stopped. Check the phone and run Clear if needed.');
        }
        const response = this.request('shutdown');
        // Also put a deadline on process exit, in case Python replies but does not finish
        // closing.
        const deadline = setTimeout(() => this.terminate(), this.stopTimeout);
        try {
          const result = await response;
          this.child.stdin.end();
          await this.closed;
          return result;
        } finally { clearTimeout(deadline); }
      } catch (error) {
        this.terminate();
        await this.closed;
        throw error;
      }
    };
    this.closing = stop();
    return this.closing;
  }
}
