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

  var done = false;
  function init() {
    if (done) return; done = true;
    addLoginLinks();
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
