import unittest

from universal_orchestrator.core.models import (
    CostConfidence,
    CostInfo,
    ProviderDescriptor,
    ProviderStatus,
)
from universal_orchestrator.errors import DuplicateError, NotFoundError
from universal_orchestrator.registry.providers import ProviderRegistry


def _provider(**overrides):
    defaults = {"name": "anthropic", "kind": "api"}
    defaults.update(overrides)
    return ProviderDescriptor(**defaults)


class RegistrationTests(unittest.TestCase):
    def test_register_then_get(self):
        registry = ProviderRegistry()
        provider = registry.register(_provider())
        self.assertIs(registry.get(provider.provider_id), provider)

    def test_duplicate_provider_id_raises(self):
        registry = ProviderRegistry()
        provider = _provider()
        registry.register(provider)
        with self.assertRaises(DuplicateError):
            registry.register(provider)

    def test_rejects_empty_name_or_kind(self):
        registry = ProviderRegistry()
        with self.assertRaises(ValueError):
            registry.register(_provider(name=""))
        with self.assertRaises(ValueError):
            registry.register(_provider(kind=""))

    def test_registering_forces_unverified_and_unknown_status(self):
        registry = ProviderRegistry()
        provider = registry.register(
            _provider(verified=True, status=ProviderStatus.AVAILABLE)
        )
        self.assertFalse(provider.verified)
        self.assertIs(provider.status, ProviderStatus.UNKNOWN)


class ProviderValidationTests(unittest.TestCase):
    def test_list_filters_by_kind(self):
        registry = ProviderRegistry()
        api_provider = registry.register(_provider(name="anthropic", kind="api"))
        registry.register(_provider(name="claude-cli", kind="cli"))

        self.assertEqual(
            [p.provider_id for p in registry.list(kind="api")], [api_provider.provider_id]
        )


class ModelMetadataTests(unittest.TestCase):
    def test_model_id_and_capabilities_are_stored_as_given(self):
        registry = ProviderRegistry()
        provider = registry.register(
            _provider(model_id="claude-opus-5", capabilities=["planning", "review"])
        )
        self.assertEqual(provider.model_id, "claude-opus-5")
        self.assertEqual(provider.capabilities, ["planning", "review"])


class VerificationStatusTests(unittest.TestCase):
    def test_set_verified_and_set_status_round_trip(self):
        registry = ProviderRegistry()
        provider = registry.register(_provider())
        registry.set_verified(provider.provider_id, True)
        registry.set_status(provider.provider_id, ProviderStatus.AVAILABLE)

        updated = registry.get(provider.provider_id)
        self.assertTrue(updated.verified)
        self.assertIs(updated.status, ProviderStatus.AVAILABLE)

    def test_is_routable_requires_both_verified_and_available(self):
        registry = ProviderRegistry()
        provider = registry.register(_provider())
        self.assertFalse(registry.is_routable(provider.provider_id))

        registry.set_verified(provider.provider_id, True)
        self.assertFalse(registry.is_routable(provider.provider_id))  # status still UNKNOWN

        registry.set_status(provider.provider_id, ProviderStatus.AVAILABLE)
        self.assertTrue(registry.is_routable(provider.provider_id))

    def test_unknown_provider_id_raises_not_found(self):
        registry = ProviderRegistry()
        with self.assertRaises(NotFoundError):
            registry.set_verified("prov_missing", True)


class MissingCredentialTests(unittest.TestCase):
    def test_provider_descriptor_has_no_credential_field(self):
        slots = ProviderDescriptor.__slots__
        for forbidden in ("api_key", "token", "credentials", "secret"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, slots)

    def test_registration_succeeds_with_no_credential_information_at_all(self):
        registry = ProviderRegistry()
        provider = registry.register(_provider())
        self.assertFalse(provider.verified)


class UnknownCostDataTests(unittest.TestCase):
    def test_default_cost_confidence_is_unknown(self):
        provider = _provider()
        self.assertIs(provider.cost.confidence, CostConfidence.UNKNOWN)
        self.assertIsNone(provider.cost.input_per_1k_tokens)

    def test_estimated_cost_is_distinguishable_from_verified(self):
        estimated = _provider(
            cost=CostInfo(confidence=CostConfidence.ESTIMATED, input_per_1k_tokens=3.0)
        )
        verified = _provider(
            cost=CostInfo(confidence=CostConfidence.VERIFIED, input_per_1k_tokens=3.0)
        )
        self.assertIsNot(estimated.cost.confidence, verified.cost.confidence)


if __name__ == "__main__":
    unittest.main()
