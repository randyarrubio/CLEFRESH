(function () {
  'use strict';

  var EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  var PW_RULES = [
    { key: 'len',    test: function (v) { return v.length >= 8; }, label: '8+ characters' },
    { key: 'upper',  test: function (v) { return /[A-Z]/.test(v); }, label: 'an uppercase letter' },
    { key: 'lower',  test: function (v) { return /[a-z]/.test(v); }, label: 'a lowercase letter' },
    { key: 'digit',  test: function (v) { return /\d/.test(v); }, label: 'a number' },
    { key: 'symbol', test: function (v) { return /[^A-Za-z0-9]/.test(v); }, label: 'a symbol' }
  ];

  function getGroup(input) {
    return input.closest('.form-group') || input.parentElement;
  }

  function ensureErrorEl(input) {
    var group = getGroup(input);
    var el = group.querySelector(':scope > .cf-inline-error');
    if (!el) {
      el = document.createElement('span');
      el.className = 'cf-inline-error';
      group.appendChild(el);
    }
    return el;
  }

  function setError(input, msg) {
    var el = ensureErrorEl(input);
    if (msg) {
      el.textContent = msg;
      el.classList.add('is-visible');
      input.classList.add('cf-invalid');
      input.classList.remove('cf-valid');
    } else {
      el.textContent = '';
      el.classList.remove('is-visible');
      input.classList.remove('cf-invalid');
      if (input.value) {
        input.classList.add('cf-valid');
      } else {
        input.classList.remove('cf-valid');
      }
    }
  }

  function validateEmail(input, opts) {
    opts = opts || {};
    var v = (input.value || '').trim();
    if (!v) {
      if (opts.requiredOnSubmit) {
        setError(input, 'Email address is required.');
        return false;
      }
      setError(input, '');
      return false;
    }
    if (!EMAIL_RE.test(v)) {
      setError(input, 'Enter a valid email address.');
      return false;
    }
    setError(input, '');
    return true;
  }

  function evaluatePassword(v) {
    var passed = [];
    var missing = [];
    PW_RULES.forEach(function (r) {
      if (r.test(v)) passed.push(r.key);
      else missing.push(r.label);
    });
    return { passed: passed, missing: missing };
  }

  function ensureMeter(input) {
    var group = getGroup(input);
    var meter = group.querySelector(':scope > .cf-pw-meter');
    if (!meter) {
      meter = document.createElement('div');
      meter.className = 'cf-pw-meter';
      meter.innerHTML =
        '<div class="cf-pw-bar"><span></span></div>' +
        '<div class="cf-pw-label" aria-live="polite"></div>';
      group.insertBefore(meter, ensureErrorEl(input));
    }
    return meter;
  }

  function updateMeter(input, result) {
    var meter = ensureMeter(input);
    var fill = meter.querySelector('.cf-pw-bar span');
    var label = meter.querySelector('.cf-pw-label');
    var n = result.passed.length;
    var pct = (n / PW_RULES.length) * 100;
    var strength = n <= 2 ? 'weak' : (n === 3 ? 'fair' : (n === 4 ? 'good' : 'strong'));
    var labels = { weak: 'Weak', fair: 'Fair', good: 'Good', strong: 'Strong' };
    fill.style.width = pct + '%';
    meter.setAttribute('data-strength', strength);
    label.textContent = input.value ? labels[strength] : '';
  }

  function validatePassword(input, opts) {
    opts = opts || {};
    var v = input.value || '';
    if (!v) {
      if (opts.withMeter) updateMeter(input, { passed: [], missing: [] });
      if (opts.requiredOnSubmit) {
        setError(input, 'Password is required.');
        return false;
      }
      setError(input, '');
      return false;
    }
    var result = evaluatePassword(v);
    if (opts.withMeter) updateMeter(input, result);
    if (result.missing.length) {
      setError(input, 'Password must contain ' + result.missing.join(', ') + '.');
      return false;
    }
    setError(input, '');
    return true;
  }

  function validateLoginPassword(input, opts) {
    opts = opts || {};
    var v = input.value || '';
    if (!v) {
      if (opts.requiredOnSubmit) {
        setError(input, 'Password is required.');
        return false;
      }
      setError(input, '');
      return false;
    }
    if (v.length < 8) {
      setError(input, 'Password must be at least 8 characters.');
      return false;
    }
    setError(input, '');
    return true;
  }

  function validateMatch(p1, p2, opts) {
    opts = opts || {};
    if (!p2.value) {
      if (opts.requiredOnSubmit) {
        setError(p2, 'Please confirm your password.');
        return false;
      }
      setError(p2, '');
      return false;
    }
    if (p1.value !== p2.value) {
      setError(p2, 'Passwords do not match.');
      return false;
    }
    setError(p2, '');
    return true;
  }

  // Debounced live duplicate-email check
  var emailCheckTimers = new WeakMap();
  function liveCheckDuplicateEmail(input) {
    var prior = emailCheckTimers.get(input);
    if (prior) clearTimeout(prior);
    if (!validateEmail(input)) return;
    var t = setTimeout(function () {
      var value = (input.value || '').trim();
      fetch('/auth/check-email/?email=' + encodeURIComponent(value), {
        credentials: 'same-origin',
        headers: { 'X-Requested-With': 'XMLHttpRequest' }
      })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data) return;
          // Only update if the field value hasn't changed since the request
          if ((input.value || '').trim() !== value) return;
          if (data.exists) {
            setError(input, 'An account with this email already exists.');
          }
        })
        .catch(function () { /* network errors — ignore, server still validates */ });
    }, 450);
    emailCheckTimers.set(input, t);
  }

  function wireForm(form) {
    if (form.dataset.cfWired === '1') return;
    form.dataset.cfWired = '1';

    var loginEmail = form.querySelector('input[name="username"]');
    var loginPw = !form.querySelector('input[name="password1"]')
      ? form.querySelector('input[name="password"]')
      : null;
    var regEmail = form.querySelector('input[name="email"]');
    var regPw1 = form.querySelector('input[name="password1"]');
    var regPw2 = form.querySelector('input[name="password2"]');

    if (loginEmail && loginEmail.type === 'email') {
      loginEmail.addEventListener('input', function () { validateEmail(loginEmail); });
      loginEmail.addEventListener('blur', function () { validateEmail(loginEmail); });
    }
    if (loginPw) {
      loginPw.addEventListener('input', function () { validateLoginPassword(loginPw); });
      loginPw.addEventListener('blur', function () { validateLoginPassword(loginPw); });
    }
    if (regEmail) {
      regEmail.addEventListener('input', function () {
        if (validateEmail(regEmail)) liveCheckDuplicateEmail(regEmail);
      });
      regEmail.addEventListener('blur', function () {
        if (validateEmail(regEmail)) liveCheckDuplicateEmail(regEmail);
      });
    }
    if (regPw1) {
      ensureMeter(regPw1);
      regPw1.addEventListener('input', function () {
        validatePassword(regPw1, { withMeter: true });
        if (regPw2 && regPw2.value) validateMatch(regPw1, regPw2);
      });
      regPw1.addEventListener('blur', function () {
        validatePassword(regPw1, { withMeter: true });
      });
    }
    if (regPw2) {
      regPw2.addEventListener('input', function () { validateMatch(regPw1, regPw2); });
      regPw2.addEventListener('blur', function () { validateMatch(regPw1, regPw2); });
    }

    form.addEventListener('submit', function (e) {
      var ok = true;
      if (loginEmail && loginEmail.type === 'email') {
        if (!validateEmail(loginEmail, { requiredOnSubmit: true })) ok = false;
      }
      if (loginPw) {
        if (!validateLoginPassword(loginPw, { requiredOnSubmit: true })) ok = false;
      }
      if (regEmail) {
        if (!validateEmail(regEmail, { requiredOnSubmit: true })) ok = false;
      }
      if (regPw1) {
        if (!validatePassword(regPw1, { withMeter: true, requiredOnSubmit: true })) ok = false;
      }
      if (regPw1 && regPw2) {
        if (!validateMatch(regPw1, regPw2, { requiredOnSubmit: true })) ok = false;
      }
      if (!ok) {
        e.preventDefault();
        e.stopPropagation();  // prevent base.html "Please wait…" loading state on invalid submit
        var firstInvalid = form.querySelector('.cf-invalid');
        if (firstInvalid && typeof firstInvalid.focus === 'function') firstInvalid.focus();
      }
    });
  }

  function wireAll() {
    document.querySelectorAll('form').forEach(function (form) {
      var hasLogin = form.querySelector('input[name="username"][type="email"]')
        && form.querySelector('input[name="password"]');
      var hasRegister = form.querySelector('input[name="password1"]')
        && form.querySelector('input[name="password2"]');
      if (hasLogin || hasRegister) wireForm(form);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wireAll);
  } else {
    wireAll();
  }
})();
