from __future__ import annotations


def test_reprice_email_delivery_is_deduped_after_a_successful_send(tmp_path):
    from scripts import batch_smart_reprice as reprice

    ledger = tmp_path / "reprice-email.db"

    assert reprice.claim_reprice_email_delivery(
        business_date="2026-07-20", db_path=ledger
    ) is True
    reprice.mark_reprice_email_delivery(
        business_date="2026-07-20", delivered=True, db_path=ledger
    )

    assert reprice.claim_reprice_email_delivery(
        business_date="2026-07-20", db_path=ledger
    ) is False


def test_reprice_email_delivery_can_retry_after_smtp_failure(tmp_path):
    from scripts import batch_smart_reprice as reprice

    ledger = tmp_path / "reprice-email.db"

    assert reprice.claim_reprice_email_delivery(
        business_date="2026-07-20", db_path=ledger
    ) is True


def test_reprice_email_delivery_cannot_be_claimed_twice_while_sending(tmp_path):
    from scripts import batch_smart_reprice as reprice

    ledger = tmp_path / "reprice-email.db"

    assert reprice.claim_reprice_email_delivery(
        business_date="2026-07-20", db_path=ledger
    ) is True
    assert reprice.claim_reprice_email_delivery(
        business_date="2026-07-20", db_path=ledger
    ) is False
    reprice.mark_reprice_email_delivery(
        business_date="2026-07-20", delivered=False, db_path=ledger
    )

    assert reprice.claim_reprice_email_delivery(
        business_date="2026-07-20", db_path=ledger
    ) is True
