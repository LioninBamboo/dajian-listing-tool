"""Guard the SOCKS extra that MI drafting needs behind the local proxy."""


def test_httpx_socks_extra_is_installed():
    import httpx
    import socksio

    assert httpx.__version__
    assert socksio.__version__
