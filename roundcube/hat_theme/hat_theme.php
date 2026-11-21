<?php
/**
 * Хат — фирменное оформление Roundcube в стиле Mail.ru:
 * светлое меню, логотип «Хат», кнопка «Написать письмо» над папками,
 * письма одной широкой лентой, письмо открывается целиком.
 * Обновления Roundcube этот плагин не затирают.
 */
class hat_theme extends rcube_plugin
{
    public $task = '.*';   // на всех страницах, включая вход

    public function init()
    {
        $this->include_stylesheet('hat.css');
        $this->include_script('hat.js');
    }
}
