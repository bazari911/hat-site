<?php
/**
 * Хат — плагин Roundcube:
 *  1) оформление в стиле Mail.ru (hat.css, hat.js);
 *  2) служебный адрес для главной kazxat.kz: «кто вошёл и сколько непрочитанных»;
 *  3) аватарки пользователей @kazxat.kz: загрузка в «Настройки → Аватар»,
 *     показ в списке писем, в открытом письме и на главной.
 * Обновления Roundcube этот плагин не затирают.
 */
class hat_theme extends rcube_plugin
{
    public $task = '.*';   // на всех страницах, включая вход

    // Сайты, которым разрешено узнавать, вошёл ли человек в почту
    private $allowed_origins = ['https://kazxat.kz', 'https://www.kazxat.kz'];
    // Чьи аватарки показываем (только наши пользователи)
    private $domain = 'kazxat.kz';
    // Предел размера одного фото после сжатия
    private $max_bytes = 150000;

    public function init()
    {
        $this->include_stylesheet('hat.css');
        $this->include_script('hat.js');

        // https://mail.kazxat.kz/rc/?_task=mail&_action=plugin.hat_status
        $this->register_action('plugin.hat_status', [$this, 'hat_status']);

        // Аватарки
        $this->add_hook('contact_photo', [$this, 'contact_photo']);
        $this->add_hook('preferences_sections_list', [$this, 'prefs_sections']);
        $this->add_hook('preferences_list', [$this, 'prefs_list']);
        $this->add_hook('preferences_save', [$this, 'prefs_save']);
    }

    /* ---------------- статус для главной ---------------- */

    public function hat_status()
    {
        $rc = rcmail::get_instance();

        $origin = $_SERVER['HTTP_ORIGIN'] ?? '';
        if (in_array($origin, $this->allowed_origins, true)) {
            header('Access-Control-Allow-Origin: ' . $origin);
            header('Access-Control-Allow-Credentials: true');
        }
        header('Vary: Origin');
        header('Content-Type: application/json; charset=utf-8');
        header('Cache-Control: no-store, private');
        header('X-Content-Type-Options: nosniff');

        $unread = 0;
        try {
            $unread = (int) $rc->get_storage()->count('INBOX', 'UNSEEN');
        } catch (\Throwable $e) {
            $unread = 0;
        }
        $prefs = $rc->user ? (array) $rc->user->get_prefs() : [];

        echo json_encode([
            'user'   => (string) $rc->get_user_name(),
            'unread' => $unread,
            'avatar' => !empty($prefs['hat_avatar']),
        ], JSON_UNESCAPED_UNICODE);
        exit;
    }

    /* ---------------- аватарки: показ ---------------- */

    /**
     * Roundcube спрашивает фото отправителя (в открытом письме, в адресной книге).
     * Если у отправителя @kazxat.kz есть аватар — отдаём его.
     */
    public function contact_photo($args)
    {
        if (!empty($args['data']) || !empty($args['url'])) {
            return $args;
        }
        $email = strtolower(trim((string) ($args['email'] ?? '')));
        if ($email === '' && !empty($args['record']['email'])) {
            $e = $args['record']['email'];
            $email = strtolower(trim(is_array($e) ? (string) reset($e) : (string) $e));
        }
        if (!$this->is_ours($email)) {
            return $args;
        }
        $bin = $this->avatar_binary($email);
        if ($bin !== null) {
            $args['data'] = $bin;
        }
        return $args;
    }

    private function is_ours($email)
    {
        return (bool) preg_match('/^[a-z0-9._+-]+@' . preg_quote($this->domain, '/') . '$/', $email);
    }

    /** Аватар пользователя по адресу: data:URI из его настроек или null */
    private function avatar_uri($email)
    {
        $rc = rcmail::get_instance();
        if ($rc->user && strtolower((string) $rc->user->get_username()) === $email) {
            $p = (array) $rc->user->get_prefs();
            return !empty($p['hat_avatar']) ? $p['hat_avatar'] : null;
        }
        $db  = $rc->get_dbh();
        $res = $db->query('SELECT preferences FROM ' . $db->table_name('users', true) . ' WHERE LOWER(username) = ?', $email);
        $row = $db->fetch_assoc($res);
        if (!$row || empty($row['preferences'])) {
            return null;
        }
        $p = @unserialize($row['preferences'], ['allowed_classes' => false]);
        return (is_array($p) && !empty($p['hat_avatar'])) ? $p['hat_avatar'] : null;
    }

    private function avatar_binary($email)
    {
        $uri = $this->avatar_uri($email);
        if (!$uri || !preg_match('#^data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=]+)$#', $uri, $m)) {
            return null;
        }
        $bin = base64_decode($m[2], true);
        return ($bin === false || $bin === '') ? null : $bin;
    }

    /* ---------------- аватарки: настройки ---------------- */

    public function prefs_sections($args)
    {
        $args['list']['hat_avatar'] = ['id' => 'hat_avatar', 'section' => 'Аватар'];
        return $args;
    }

    public function prefs_list($args)
    {
        if ($args['section'] !== 'hat_avatar') {
            return $args;
        }
        $rc    = rcmail::get_instance();
        $prefs = (array) $rc->user->get_prefs();
        $cur   = !empty($prefs['hat_avatar']) ? $prefs['hat_avatar'] : '';
        $name  = (string) $rc->get_user_name();
        $letter = rcube::Q(mb_strtoupper(mb_substr($name, 0, 1)));

        $html = '<div class="hat-avatar-edit">'
            . '<div class="hat-avatar-preview" id="hat-avatar-preview">'
            . ($cur ? '<img src="' . rcube::Q($cur) . '" alt="">' : '<span>' . $letter . '</span>')
            . '</div>'
            . '<div class="hat-avatar-actions">'
            . '<label for="hat-avatar-file" class="btn btn-primary hat-avatar-pick">Выбрать фото'
            . '<input type="file" id="hat-avatar-file" accept="image/jpeg,image/png,image/webp" hidden></label> '
            . '<button type="button" class="btn btn-secondary" id="hat-avatar-remove"' . ($cur ? '' : ' style="display:none"') . '>Удалить фото</button>'
            . '<input type="hidden" name="_hat_avatar" id="hat-avatar-data" value="">'
            . '<div class="hat-avatar-hint">JPG или PNG. Фото обрежется в квадрат и уменьшится. '
            . 'Его увидят пользователи @' . rcube::Q($this->domain) . ', которым вы пишете. '
            . 'После выбора нажмите «Сохранить».</div>'
            . '</div></div>';

        $args['blocks']['main'] = [
            'name'    => 'Фото профиля',
            'options' => ['avatar' => ['title' => '', 'content' => $html]],
        ];
        return $args;
    }

    public function prefs_save($args)
    {
        if ($args['section'] !== 'hat_avatar') {
            return $args;
        }
        $val = (string) rcube_utils::get_input_value('_hat_avatar', rcube_utils::INPUT_POST, true);
        if ($val === '') {
            return $args; // ничего не меняли
        }
        if ($val === '__delete__') {
            $args['prefs']['hat_avatar'] = null;
            return $args;
        }
        if (!preg_match('#^data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/=]+)$#', $val, $m)) {
            $args['abort'] = true;
            $args['message'] = 'Не удалось сохранить фото: неверный формат';
            return $args;
        }
        $bin = base64_decode($m[2], true);
        $info = $bin !== false ? @getimagesizefromstring($bin) : false;
        if ($bin === false || strlen($bin) > $this->max_bytes || !$info || $info[0] > 1024 || $info[1] > 1024) {
            $args['abort'] = true;
            $args['message'] = 'Не удалось сохранить фото: слишком большой файл или не картинка';
            return $args;
        }
        $args['prefs']['hat_avatar'] = $val;
        return $args;
    }
}
