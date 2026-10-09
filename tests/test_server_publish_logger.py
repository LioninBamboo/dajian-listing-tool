import logging


def test_server_exposes_module_logger_for_publish_flow():
    import server

    assert isinstance(server.logger, logging.Logger)
