<?php
/**
 * Хат — фирменное оформление Roundcube в стиле Mail.ru
 * + служебный адрес для главной kazxat.kz: «кто вошёл и сколько непрочитанных».
 * Обновления Roundcube этот плагин не затирают.
 */
class hat_theme extends rcube_plugin
{
    public $task = '.*';   // на всех страницах, включая вход

    // Сайты, которым разрешено узнавать, вошёл ли человек в почту
    private $allowed_origins = ['https://kazxat.kz', 'https://www.kazxat.kz'];

    public function init()
    {
        $this->include_stylesheet('hat.css');
        $this->include_script('hat.js');
        // https://mail.kazxat.kz/rc/?_task=mail&_action=plugin.hat_status
        $this->register_action('plugin.hat_status', [$this, 'hat_status']);
    }

    /**
     * Отдаёт адрес вошедшего пользователя и число непрочитанных во «Входящих».
     * Сработает только для вошедшего: иначе Roundcube покажет страницу входа,
     * и главная поймёт, что человек не вошёл.
     */
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

        echo json_encode(['user' => (string) $rc->get_user_name(), 'unread' => $unread], JSON_UNESCAPED_UNICODE);
        exit;
    }
}
