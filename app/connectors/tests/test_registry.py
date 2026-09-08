from connectors import registry

_GATED_PLATFORM_SLUGS = {"instagram", "facebook", "threads", "linkedin", "x", "tiktok", "youtube"}


def test_all_gated_platforms_are_registered():
    assert _GATED_PLATFORM_SLUGS <= set(registry.all_slugs())


def test_byo_app_connectors_are_flagged_and_managed_ones_are_not():
    byo = {"linkedin", "x"}
    for slug in _GATED_PLATFORM_SLUGS:
        connector = registry.get(slug)
        assert connector.requires_own_app is (slug in byo), slug


def test_every_gated_connector_declares_capabilities_and_slug_matches():
    for slug in _GATED_PLATFORM_SLUGS:
        connector = registry.get(slug)
        assert connector.slug == slug
        assert connector.capabilities.slug == slug
        assert connector.capabilities.post_kinds
