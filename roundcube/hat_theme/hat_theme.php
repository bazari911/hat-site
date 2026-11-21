<?php
/**
 * Хат — фирменное оформление Roundcube (цвета и логотип «Хат» поверх темы Elastic).
 * Обновления Roundcube этот плагин не затирают.
 */
class hat_theme extends rcube_plugin
{
    public $task = '.*';   // на всех страницах, включая вход

    public function init()
    {
        $this->include_stylesheet('hat.css');
    }
}
