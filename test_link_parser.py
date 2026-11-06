from app.funpay_worker import extract_single_url

def test_links():
    assert extract_single_url("https://www.tiktok.com/@user/video/123") == "https://www.tiktok.com/@user/video/123"
    assert extract_single_url("Вот ссылка https://t.me/channel") == "https://t.me/channel"
    assert extract_single_url("не ссылка") is None
    assert extract_single_url("https://t.me/a https://t.me/b") is None
    assert extract_single_url("https://") is None
    print("Проверка парсера ссылок пройдена.")

if __name__ == "__main__":
    test_links()
