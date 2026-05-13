from core.crawler import Crawler
from reporting import console


def test_console_exports_runtime_functions_used_by_crawler():
    required = [
        "print_action",
        "print_crawl_complete",
        "print_element_count",
        "print_error_block",
        "print_form_group_result",
        "print_iframe_found",
        "print_links_enqueued",
        "print_login_required",
        "print_manifest_saved",
        "print_nav_failed",
        "print_section_header",
    ]

    for name in required:
        assert hasattr(console, name)


def test_crawler_normalize_preserves_http_scheme():
    normalized = Crawler._normalize(object(), "http://example.test/path/#section")
    assert normalized == "http://example.test/path"
