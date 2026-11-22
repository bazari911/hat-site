/* Хат — поведение почты в стиле Mail.ru:
   аватары с буквой, кнопка «Написать письмо» над папками,
   письмо открывается целиком по одному клику. */
(function () {
  var COLORS = ["#00AFCA", "#e67e22", "#8e44ad", "#16a085", "#c0392b", "#2c7be5", "#d4a017", "#27ae60"];
  function color(s) { var n = 0; for (var i = 0; i < s.length; i++) n += s.charCodeAt(i); return COLORS[n % COLORS.length]; }
  function wide() { return window.innerWidth >= 1024; }

  function decorate() {
    var rows = document.querySelectorAll('#messagelist tbody tr');
    for (var i = 0; i < rows.length; i++) {
      var td = rows[i].querySelector('td.subject');
      if (!td || td.querySelector('.hat-ava')) continue;
      var f = td.querySelector('span.fromto');
      var name = ((f && f.textContent) || '?').trim() || '?';
      var s = document.createElement('span');
      s.className = 'hat-ava';
      s.textContent = name.charAt(0).toUpperCase();
      s.style.background = color(name);
      td.insertBefore(s, td.firstChild);
      // фото пользователя @kazxat.kz, если он его загрузил (иначе остаётся буква)
      var email = senderEmail(td);
      if (email && /@kazxat\.kz$/i.test(email) && window.rcmail && rcmail.url) {
        var img = document.createElement('img');
        img.alt = '';
        img.loading = 'lazy';
        img.onerror = function () { this.remove(); };
        img.src = rcmail.url('addressbook/photo', { _email: email, _error: 1 });
        s.appendChild(img);
      }
    }
  }

  function senderEmail(td) {
    var el = td.querySelector('span.fromto [title*="@"]');
    var t = el ? el.getAttribute('title') : '';
    if (!t) { var f = td.querySelector('span.fromto'); t = f ? f.textContent : ''; }
    var m = (t || '').match(/[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+/);
    return m ? m[0].toLowerCase() : '';
  }

  /* Настройки → Аватар: выбор фото, обрезка в квадрат 160×160, сжатие в JPEG */
  function avatarSettings() {
    var file = document.getElementById('hat-avatar-file');
    if (!file) return;
    var data = document.getElementById('hat-avatar-data');
    var prev = document.getElementById('hat-avatar-preview');
    var del = document.getElementById('hat-avatar-remove');
    file.addEventListener('change', function () {
      var f = file.files && file.files[0];
      if (!f) return;
      if (!/^image\//.test(f.type)) { alert('Выберите картинку (JPG или PNG)'); return; }
      var url = URL.createObjectURL(f);
      var im = new Image();
      im.onload = function () {
        var S = 160, side = Math.min(im.width, im.height);
        var c = document.createElement('canvas'); c.width = S; c.height = S;
        c.getContext('2d').drawImage(im, (im.width - side) / 2, (im.height - side) / 2, side, side, 0, 0, S, S);
        var out = c.toDataURL('image/jpeg', 0.85);
        URL.revokeObjectURL(url);
        data.value = out;
        prev.innerHTML = '';
        var i = document.createElement('img'); i.src = out; i.alt = ''; prev.appendChild(i);
        if (del) del.style.display = '';
      };
      im.onerror = function () { alert('Не удалось открыть картинку'); };
      im.src = url;
    });
    if (del) del.addEventListener('click', function () {
      data.value = '__delete__';
      prev.innerHTML = '<span>?</span>';
      del.style.display = 'none';
    });
  }

  // Логотип «Хат» ведёт на главную kazxat.kz
  var HOME = 'https://kazxat.kz/';
  function addHomeLinks() {
    var head = document.querySelector('#layout-sidebar > .header');
    if (head && !head.querySelector('.hat-home')) {
      var a = document.createElement('a');
      a.className = 'hat-home';
      a.href = HOME;
      a.title = 'Хат — на главную';
      a.setAttribute('aria-label', 'Хат — на главную');
      head.insertBefore(a, head.firstChild);
    }
    var logos = document.querySelectorAll('#logo');
    for (var i = 0; i < logos.length; i++) {
      var img = logos[i];
      if (img.closest('a')) continue;
      var link = document.createElement('a');
      link.href = HOME;
      link.title = 'Хат — на главную';
      link.className = 'hat-logo-link';
      img.parentNode.insertBefore(link, img);
      link.appendChild(img);
    }
  }

  /* ===== Личный кабинет (как у Mail.ru): панель по аватару внизу левой полосы ===== */
  function fmtSize(b) {
    if (!b) return '0 МБ';
    var gb = b / 1073741824;
    if (gb >= 1) return (Math.round(gb * 10) / 10).toString().replace('.', ',') + ' ГБ';
    return Math.max(1, Math.round(b / 1048576)) + ' МБ';
  }
  function esc(t) { var d = document.createElement('div'); d.textContent = t == null ? '' : String(t); return d.innerHTML; }

  // Ссылки кабинета; base — адрес почты ('./' внутри почты)
  window.hatAccountHTML = function (d, base, photoUrl) {
    var local = (d.user || '').split('@')[0];
    var name = d.name || local;
    var letter = esc(name.charAt(0).toUpperCase());
    var pct = d.quota_total ? Math.min(100, Math.round(d.quota_used * 100 / d.quota_total)) : 0;
    var quota = d.quota_total ? ('Занято ' + fmtSize(d.quota_used) + ' из ' + fmtSize(d.quota_total)) : ('Занято ' + fmtSize(d.quota_used));
    var ava = '<div class="acc-ava" style="background:' + color(name) + '">' + letter +
      (d.avatar && photoUrl ? '<img src="' + esc(photoUrl) + '" alt="" onerror="this.remove()">' : '') + '</div>';
    return ava +
      '<div class="acc-name">' + esc(name) + '</div>' +
      '<div class="acc-mail">' + esc(d.user) + '</div>' +
      '<div class="acc-quota"><div>' + quota + '</div><div class="acc-bar"><i style="width:' + pct + '%"></i></div></div>' +
      '<div class="acc-links">' +
      '<a href="' + base + '?_task=settings&_action=identities"><span>👤</span>Личные данные</a>' +
      '<a href="' + base + '?_task=settings&_action=preferences&_section=hat_avatar"><span>🖼️</span>Фото профиля</a>' +
      '<a href="' + base + '?_task=settings&_action=plugin.password"><span>🔒</span>Пароль и безопасность</a>' +
      '<a href="' + base + '?_task=settings"><span>⚙️</span>Настройки почты</a>' +
      (d.logout ? '<a href="' + esc(d.logout) + '" class="acc-out"><span>🚪</span>Выйти</a>' : '') +
      '</div>';
  };

  function addAccount() {
    var box = document.querySelector('#layout-menu .special-buttons');
    if (!box || document.querySelector('.hat-acc-btn') || !window.rcmail || !rcmail.env.task || rcmail.env.task === 'login' || rcmail.env.task === 'logout') return;
    var btn = document.createElement('a');
    btn.href = '#';
    btn.className = 'hat-acc-btn';
    btn.title = 'Личный кабинет';
    var user = (rcmail.env.username || '') + '';
    btn.innerHTML = '<span class="hat-acc-mini">' + esc((user.charAt(0) || '?').toUpperCase()) + '</span><span class="inner">Кабинет</span>';
    btn.querySelector('.hat-acc-mini').style.background = color(user || '?');
    box.insertBefore(btn, box.firstChild);

    // буква / фото на кнопке кабинета
    fetch('./?_task=mail&_action=plugin.hat_status', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var nm = d.name || (d.user || '').split('@')[0] || '?';
        var mini = btn.querySelector('.hat-acc-mini');
        mini.firstChild.nodeValue = nm.charAt(0).toUpperCase();
        mini.style.background = color(nm);
        if (d.avatar) { var i = new Image(); i.alt = ''; i.onerror = function () { this.remove(); }; i.src = './?_task=addressbook&_action=photo&_error=1&_email=' + encodeURIComponent(d.user); mini.appendChild(i); }
      }).catch(function () {});

    var panel = document.createElement('div');
    panel.className = 'hat-acc';
    panel.hidden = true;
    document.body.appendChild(panel);

    btn.addEventListener('click', function (e) {
      e.preventDefault(); e.stopPropagation();
      if (!panel.hidden) { panel.hidden = true; return; }
      panel.innerHTML = '<div class="acc-loading">Загрузка…</div>';
      panel.hidden = false;
      fetch('./?_task=mail&_action=plugin.hat_status', { credentials: 'same-origin', cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var photo = './?_task=addressbook&_action=photo&_error=1&_email=' + encodeURIComponent(d.user);
          panel.innerHTML = window.hatAccountHTML(d, './', photo);
        })
        .catch(function () { panel.innerHTML = '<div class="acc-loading">Не удалось загрузить данные</div>'; });
    });
    document.addEventListener('click', function (e) { if (!panel.hidden && !panel.contains(e.target)) panel.hidden = true; });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') panel.hidden = true; });
  }

  // Открыть раздел настроек по ссылке ?_section=… (например, «Фото профиля»)
  function openSection() {
    if (!window.rcmail || rcmail.env.task !== 'settings' || rcmail.env.action !== 'preferences') return;
    var m = location.search.match(/[?&]_section=([a-z_]+)/);
    if (m && rcmail.sections_list) { try { rcmail.sections_list.select(m[1]); } catch (e) {} }
  }

  function addCompose() {
    var sidebar = document.getElementById('layout-sidebar');
    if (!sidebar || document.querySelector('.hat-compose')) return;
    var src = document.querySelector('#layout-menu a.compose');
    var head = sidebar.querySelector(':scope > .header');
    if (!src || !head) return;
    var a = document.createElement('a');
    a.className = 'hat-compose';
    a.href = src.getAttribute('href') || '#';
    a.setAttribute('onclick', src.getAttribute('onclick') || '');
    a.textContent = '✏️ Написать письмо';
    head.parentNode.insertBefore(a, head.nextSibling);
  }

  /* Страница входа: «Забыли пароль?» слева и «Поддержка» справа — одной ровной строкой под «Войти» */
  function addLoginLinks() {
    var form = document.getElementById('login-form');
    if (!form || form.querySelector('.hat-login-links')) return;
    var row = document.createElement('div');
    row.className = 'hat-login-links';
    row.innerHTML = '<a href="https://kazxat.kz/auth.html#restore">Забыли пароль?</a>' +
                    '<a href="https://kazxat.kz/auth.html#support">Поддержка</a>';
    var btns = form.querySelector('.formbuttons');
    if (btns && btns.parentNode) btns.parentNode.insertBefore(row, btns.nextSibling);
    else form.appendChild(row);
    var reg = document.createElement('div');
    reg.className = 'hat-login-reg';
    reg.innerHTML = 'Нет почты? <a href="https://kazxat.kz/auth.html#register">Создать ящик @kazxat.kz</a>';
    row.parentNode.insertBefore(reg, row.nextSibling);
  }

  /* ===== Хат Диск: большие файлы уходят ссылкой (до 2 ГБ, хранятся 30 дней) ===== */
  var MB = 1048576, GB = 1073741824;
  var disk = { on: false, tok: null, tokAt: 0, busy: 0, info: null, box: null, list: null };

  function diskSize(n) {
    if (!n || n <= 0) return '0 МБ';
    if (n >= GB) return (Math.round(n / GB * 10) / 10).toString().replace('.', ',') + ' ГБ';
    if (n >= MB) return Math.max(1, Math.round(n / MB)) + ' МБ';
    return Math.max(1, Math.round(n / 1024)) + ' КБ';
  }
  function diskDate(ts) {
    var d = new Date(ts * 1000);
    return ('0' + d.getDate()).slice(-2) + '.' + ('0' + (d.getMonth() + 1)).slice(-2) + '.' + d.getFullYear();
  }
  function diskLimit() { return (window.rcmail && rcmail.env.max_filesize) || 25 * MB; }
  function diskDays() { return (disk.info && disk.info.days) || 30; }

  function diskToken(fresh) {
    if (!fresh && disk.tok && Date.now() - disk.tokAt < 3 * 3600 * 1000) return Promise.resolve(disk.tok);
    return fetch('./?_task=mail&_action=plugin.hat_disk_token', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.token) throw new Error('Хат Диск пока не включён');
        disk.tok = d.token; disk.tokAt = Date.now();
        return d.token;
      });
  }
  function diskInfo() {
    return diskToken().then(function (t) {
      return fetch('/disk/api/info', { headers: { 'X-Hat-Token': t }, cache: 'no-store' });
    }).then(function (r) { if (!r.ok) throw new Error('info ' + r.status); return r.json(); })
      .then(function (d) { disk.info = d; diskUsage(); return d; });
  }
  function diskUsage() {
    var u = disk.box && disk.box.querySelector('.hat-disk-usage');
    if (u && disk.info) u.textContent = 'Занято на Хат Диске: ' + diskSize(disk.info.used) + ' из ' + diskSize(disk.info.quota);
  }

  // вставить в письмо карточку со ссылкой
  function diskInsert(f) {
    var until = diskDate(f.expires), size = diskSize(f.size);
    var text = '\n📎 ' + f.name + ' (' + size + ') — Хат Диск\nСкачать: ' + f.url +
               '\nФайл хранится до ' + until + ', потом удалится автоматически.\n';
    var h = '<table cellpadding="0" cellspacing="0" style="border:1px solid #cdeaf1;border-radius:10px;background:#f1f9fb;margin:8px 0;font-family:Arial,sans-serif">' +
      '<tr><td style="padding:10px 12px;font-size:22px;vertical-align:middle">📎</td>' +
      '<td style="padding:10px 14px 10px 0;font-size:14px;color:#1d2a30;vertical-align:middle">' +
      '<a href="' + esc(f.url) + '" style="color:#0089a0;font-weight:bold;text-decoration:none">' + esc(f.name) + '</a> ' +
      '<span style="color:#6b7c85">(' + esc(size) + ')</span><br>' +
      '<span style="font-size:12px;color:#6b7c85">Хат Диск · файл хранится до ' + until + ', потом удалится автоматически</span>' +
      '</td></tr></table><p>&nbsp;</p>';
    try { rcmail.editor.replace({ text: text, html: h }); }
    catch (e) { try { rcmail.editor.replace(text); } catch (e2) {} }
  }

  function diskItem(file) {
    var li = document.createElement('li');
    li.className = 'hat-disk-item';
    li.innerHTML = '<div class="hd-top"><span class="hd-name"></span><span class="hd-size"></span>' +
      '<button type="button" class="hd-x" title="Отменить">✕</button></div>' +
      '<div class="hd-bar"><i></i></div><div class="hd-note"></div>';
    li.querySelector('.hd-name').textContent = file.name;
    li.querySelector('.hd-size').textContent = diskSize(file.size);
    disk.list.appendChild(li);
    return li;
  }

  function diskStart(file) {
    var li = diskItem(file), note = li.querySelector('.hd-note'), bar = li.querySelector('.hd-bar i');
    var maxf = (disk.info && disk.info.max_file) || 2 * GB;
    var x = li.querySelector('.hd-x');
    function fail(msg) {
      li.classList.add('hd-err'); note.textContent = msg; x.title = 'Убрать';
      x.onclick = function () { li.remove(); };
    }
    if (file.size > maxf) return fail('Файл больше ' + diskSize(maxf) + ' — такой не отправить даже через Хат Диск');
    if (disk.info && disk.info.used + file.size > disk.info.quota)
      return fail('На вашем Хат Диске занято ' + diskSize(disk.info.used) + ' из ' + diskSize(disk.info.quota) +
                  '. Файлы освобождают место через ' + diskDays() + ' дней после загрузки.');
    var until = diskDate(Date.now() / 1000 + diskDays() * 86400);
    note.textContent = (file.size > diskLimit() ? 'Файл больше ' + diskSize(diskLimit()) + ', поэтому отправим его ссылкой через Хат Диск. ' : '') +
      'Хранится ' + diskDays() + ' дней — до ' + until + ', потом удалится автоматически.';
    if (file.size > diskLimit() && rcmail.display_message)
      rcmail.display_message('«' + file.name + '» больше ' + diskSize(diskLimit()) + ' — отправим ссылкой через Хат Диск (хранится ' + diskDays() + ' дней)', 'notice', 8000);

    disk.busy++;
    var xhr, finished = false;
    function end() { if (!finished) { finished = true; disk.busy--; } }
    x.onclick = function () { if (xhr) xhr.abort(); end(); li.remove(); };

    // шаг 1: проверка лимитов и бронь места (отказ приходит сразу, до отправки файла)
    function reserve(t) {
      return fetch('/disk/api/start', {
        method: 'POST', cache: 'no-store',
        headers: { 'X-Hat-Token': t, 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: file.name, size: file.size })
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (r.status === 401) { var e = new Error('auth'); e.auth = true; throw e; }
          if (!r.ok || !d.uid) throw new Error(d.error || 'Хат Диск недоступен (ошибка ' + r.status + ')');
          return { t: t, uid: d.uid };
        });
      });
    }

    function send(retry) {
      diskToken(retry).then(reserve).then(function (res) {
        if (finished) return;
        var t = res.t;
        xhr = new XMLHttpRequest();
        xhr.open('POST', '/disk/api/upload');
        xhr.setRequestHeader('X-Hat-Token', t);
        xhr.setRequestHeader('X-Upload-Id', res.uid);
        xhr.setRequestHeader('Content-Type', 'application/octet-stream');
        xhr.upload.onprogress = function (e) {
          if (!e.lengthComputable) return;
          var p = Math.floor(e.loaded * 100 / e.total);
          bar.style.width = p + '%';
          li.querySelector('.hd-size').textContent = diskSize(e.loaded) + ' из ' + diskSize(file.size) + ' · ' + p + '%';
        };
        xhr.onload = function () {
          var d = {};
          try { d = JSON.parse(xhr.responseText); } catch (e) {}
          if (xhr.status === 401 && !retry) return send(true);
          end();
          if (xhr.status !== 200 || !d.url) return fail(d.error || 'Не удалось загрузить файл (ошибка ' + xhr.status + ')');
          bar.style.width = '100%';
          li.classList.add('hd-ok');
          li.querySelector('.hd-size').textContent = diskSize(d.size);
          note.innerHTML = '✓ Ссылка вставлена в письмо · хранится до <b>' + diskDate(d.expires) + '</b> · ';
          var a = document.createElement('a'); a.href = d.url; a.target = '_blank'; a.rel = 'noopener'; a.textContent = 'открыть';
          note.appendChild(a);
          x.title = 'Убрать из списка (файл останется по ссылке)';
          x.onclick = function () { li.remove(); };
          if (disk.info) { disk.info.used = d.used; diskUsage(); }
          diskInsert(d);
        };
        xhr.onerror = function () { end(); fail('Связь прервалась. Проверьте интернет и попробуйте ещё раз.'); };
        xhr.send(file);
      }).catch(function (e) {
        if (e && e.auth && !retry) return send(true);
        end(); fail((e && e.auth ? 'Войдите в почту заново' : e && e.message) || 'Хат Диск недоступен');
      });
    }
    send(false);
  }

  function addDisk() {
    if (!window.rcmail || rcmail.env.task !== 'mail' || rcmail.env.action !== 'compose') return;
    var area = document.getElementById('compose-attachments');
    if (!area || area.querySelector('.hat-disk')) return;
    diskToken().then(function () {
      disk.on = true;
      var box = document.createElement('div');
      box.className = 'hat-disk';
      box.innerHTML = '<button type="button" class="btn btn-secondary hat-disk-btn">☁️ Большой файл — Хат Диск</button>' +
        '<input type="file" multiple hidden>' +
        '<div class="hat-disk-hint">Файлы больше ' + diskSize(diskLimit()) + ' уходят ссылкой: до 2 ГБ, хранятся 30 дней.</div>' +
        '<div class="hat-disk-usage"></div><ul class="hat-disk-list"></ul>';
      var btnRow = area.querySelector('.btn.attach');
      if (btnRow && btnRow.parentNode) btnRow.parentNode.insertBefore(box, btnRow.nextSibling);
      else area.insertBefore(box, area.firstChild);
      disk.box = box; disk.list = box.querySelector('.hat-disk-list');
      var inp = box.querySelector('input[type=file]');
      box.querySelector('.hat-disk-btn').onclick = function () { inp.value = ''; inp.click(); };
      inp.onchange = function () { for (var i = 0; i < inp.files.length; i++) diskStart(inp.files[i]); inp.value = ''; };
      diskInfo().then(function (d) {
        box.querySelector('.hat-disk-hint').textContent = 'Файлы больше ' + diskSize(diskLimit()) + ' уходят ссылкой: до ' +
          diskSize(d.max_file) + ', хранятся ' + d.days + ' дней.';
      }).catch(function () {});
    }).catch(function () { /* диск не включён — почта работает как раньше */ });

    // большие файлы из «Прикрепить файл» и перетаскивания — на Хат Диск, маленькие — как обычно
    var orig = rcmail.file_upload;
    rcmail.file_upload = function (files, post_args, props) {
      var action = props && props.action;
      if (disk.on && files && files.length && (!action || action === 'upload')) {
        var big = [], small = [], lim = diskLimit();
        for (var i = 0; i < files.length; i++) (files[i].size > lim ? big : small).push(files[i]);
        if (big.length) {
          big.forEach(diskStart);
          if (!small.length) return false;
          files = small;
        }
      }
      return orig.call(this, files, post_args, props);
    };
    rcmail.addEventListener('beforesend', function () {
      if (disk.busy > 0) {
        rcmail.alert_dialog('Подождите: большой файл ещё загружается на Хат Диск. Отправить письмо можно, когда загрузка закончится.');
        return false;
      }
    });
    window.addEventListener('beforeunload', function (e) { if (disk.busy > 0) { e.preventDefault(); e.returnValue = ''; } });
  }

  var done = false;
  function init() {
    if (done) return; done = true;
    addLoginLinks();
    addDisk();
    if (document.getElementById('layout-sidebar')) document.body.classList.add('hat-has-sidebar');
    addHomeLinks();
    addCompose();
    avatarSettings();
    addAccount();
    openSection();

    if (window.rcmail && rcmail.env.task === 'mail' && !rcmail.env.action) {
      // без колонки предпросмотра: письмо открывается целиком
      if (wide()) rcmail.env.contentframe = null;
      decorate();
      rcmail.addEventListener('listupdate', decorate);
      var tbody = document.querySelector('#messagelist tbody');
      if (tbody && window.MutationObserver) new MutationObserver(decorate).observe(tbody, { childList: true });

      document.addEventListener('click', function (e) {
        if (!wide()) return;
        var tr = e.target.closest && e.target.closest('#messagelist tbody tr');
        if (!tr) return;
        if (e.target.closest('td.selection, td.flags, input, .threads, .flag') || e.ctrlKey || e.shiftKey || e.metaKey) return;
        if (document.querySelector('#messagelist.withselection')) return; // режим «Выбрать»
        var a = tr.querySelector('span.subject a');
        if (!a) return;
        e.preventDefault(); e.stopPropagation();
        location.href = a.href;
      }, true);
    }
  }

  if (document.readyState === 'complete' && window.rcmail && rcmail.env && rcmail.env.task) init();
  else if (window.rcmail && rcmail.addEventListener) rcmail.addEventListener('init', init);
  else document.addEventListener('DOMContentLoaded', init);
})();
