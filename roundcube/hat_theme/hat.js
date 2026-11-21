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

  var done = false;
  function init() {
    if (done) return; done = true;
    if (document.getElementById('layout-sidebar')) document.body.classList.add('hat-has-sidebar');
    addCompose();
    avatarSettings();

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
