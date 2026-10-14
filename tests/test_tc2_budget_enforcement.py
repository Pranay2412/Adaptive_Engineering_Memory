"""Unit and regression tests for TC2 Context-Budget Enforcement in Synapse.

Validates that knowledge-context token budgets are strictly enforced under the
authoritative tiktoken cl100k_base tokenizer across all edge cases:
- Minimum/zero/negative budget
- Small budget
- Exact fit
- One-token-under fit
- Large and unbounded budget
- Empty context
- Oversized single item
- Multiple items knapsack efficiency
- Unicode and special characters
- Deterministic repeatability
- Formatting overhead accounting
- Regression sweep: measured context never exceeds budget
- Raw greedy mode (OptimizationMode.NONE) budget enforcement
"""

import unittest
from backend.models import (
    HybridRetrievalResult,
    IntentAnalysisResult,
    OptimizationMode,
    QueryIntent,
    RelationshipHop,
)
from backend.orchestrator import AdaptiveContextPacker
from backend.token_optimizer import (
    ContextTier,
    TokenEstimator,
    TokenOptimizationEngine,
    render_candidate_item,
)
from benchmarks.token_benchmark import count_tokens_precise


def make_mock_result(
    entity: str,
    content: str,
    source: str = "code",
    item_type: str = "function",
    score: float = 0.85,
    path: str = "backend/auth.py",
) -> HybridRetrievalResult:
    """Helper to construct a realistic HybridRetrievalResult."""
    return HybridRetrievalResult(
        entity=entity,
        type=item_type,
        source=source,
        file_or_document=path,
        content_snippet=content,
        relationship_path=[RelationshipHop("1", entity, "CALLS", "2", "Dependency")],
        confidence=0.9,
        score=score,
    )


class TestTC2BudgetEnforcement(unittest.TestCase):
    """Test suite strictly validating budget enforcement and measurement consistency."""

    def setUp(self):
        self.engine = TokenOptimizationEngine(tokenizer=count_tokens_precise)
        self.intent = IntentAnalysisResult(
            primary_intent=QueryIntent.FEATURE_IMPLEMENTATION,
            confidence=0.95,
        )

        self.sample_items = [
            make_mock_result(
                entity="AuthService.verify_token",
                content="def verify_token(token: str) -> dict:\n    # Verify JWT signature with RS256 public key\n    decoded = jwt.decode(token, PUBLIC_KEY, algorithms=['RS256'])\n    return decoded",
                score=0.95,
            ),
            make_mock_result(
                entity="ADR-004: JWT RSA256 Authentication",
                content="Decision: We chose RS256 asymmetric signing over HS256 symmetric signing so that microservices can verify tokens without sharing the private secret key.",
                source="documentation",
                item_type="ADR",
                score=0.90,
                path="docs/adr/004-jwt-rs256.md",
            ),
            make_mock_result(
                entity="TokenManager.refresh",
                content="def refresh_session(refresh_token: str) -> TokenPair:\n    session = db.query(Session).filter_by(token=refresh_token).first()\n    if not session or session.is_expired(): raise AuthError('Expired')\n    return issue_token_pair(session.user_id)",
                score=0.82,
            ),
            make_mock_result(
                entity="Memory: RS256 Key Rotation Incident",
                content="During the October maintenance window, public keys were rotated in Consul. Ensure all instances refresh their public key cache on verification failure.",
                source="team_memory",
                item_type="MemoryItem",
                score=0.78,
                path="team_memory/incidents/042.md",
            ),
            make_mock_result(
                entity="UserModel.get_by_id",
                content="class User(BaseModel):\n    id: UUID\n    email: str\n    roles: list[str]\n    is_active: bool = True",
                score=0.70,
                path="backend/models/user.py",
            ),
        ]

    def test_zero_or_negative_budget(self):
        """Zero or negative budget must result in empty context and 0 tokens."""
        for b in [0, -1, -50]:
            res = self.engine.optimize(
                user_query="How does authentication work?",
                ranked_context_items=self.sample_items,
                max_token_budget=b,
                intent_result=self.intent,
                mode=OptimizationMode.DETERMINISTIC,
                tokenizer=count_tokens_precise,
            )
            self.assertEqual(res.optimized_context, "")
            self.assertEqual(res.estimated_input_tokens, 0)
            self.assertEqual(len(res.items_retained), 0)
            self.assertLessEqual(count_tokens_precise(res.optimized_context), max(0, b))

    def test_small_budget(self):
        """Very small budgets (20, 40 tokens) must not overflow."""
        for b in [20, 40]:
            res = self.engine.optimize(
                user_query="How does authentication work?",
                ranked_context_items=self.sample_items,
                max_token_budget=b,
                intent_result=self.intent,
                mode=OptimizationMode.DETERMINISTIC,
                tokenizer=count_tokens_precise,
            )
            measured = count_tokens_precise(res.optimized_context)
            self.assertLessEqual(
                measured,
                b,
                f"Small budget {b} overflowed: measured {measured} tokens"
            )

    def test_exact_fit(self):
        """When an item's exact rendered context equals the budget, it must fit with 100% utilization."""
        single_item = [self.sample_items[0]]
        res_baseline = self.engine.optimize(
            user_query="How does authentication work?",
            ranked_context_items=single_item,
            max_token_budget=100,
            intent_result=self.intent,
            mode=OptimizationMode.DETERMINISTIC,
            tokenizer=count_tokens_precise,
        )
        exact_tok = count_tokens_precise(res_baseline.optimized_context)

        res = self.engine.optimize(
            user_query="How does authentication work?",
            ranked_context_items=single_item,
            max_token_budget=exact_tok,
            intent_result=self.intent,
            mode=OptimizationMode.DETERMINISTIC,
            tokenizer=count_tokens_precise,
        )
        measured = count_tokens_precise(res.optimized_context)
        self.assertLessEqual(measured, exact_tok)
        self.assertEqual(measured, exact_tok)
        utilization = (measured / exact_tok) * 100.0
        self.assertAlmostEqual(utilization, 100.0, places=1)

    def test_one_token_under_fit(self):
        """If budget is 1 token less than the fit, it must drop to a lower tier or omit, never overflow."""
        single_item = [self.sample_items[0]]
        res_baseline = self.engine.optimize(
            user_query="How does authentication work?",
            ranked_context_items=single_item,
            max_token_budget=100,
            intent_result=self.intent,
            mode=OptimizationMode.DETERMINISTIC,
            tokenizer=count_tokens_precise,
        )
        exact_tok = count_tokens_precise(res_baseline.optimized_context)
        budget = exact_tok - 1

        res = self.engine.optimize(
            user_query="How does authentication work?",
            ranked_context_items=single_item,
            max_token_budget=budget,
            intent_result=self.intent,
            mode=OptimizationMode.DETERMINISTIC,
            tokenizer=count_tokens_precise,
        )
        measured = count_tokens_precise(res.optimized_context)
        self.assertLessEqual(
            measured,
            budget,
            f"One-token-under budget {budget} overflowed: measured {measured}"
        )

    def test_large_and_unbounded_budget(self):
        """Large budget must pack all available items up to the candidate pool size."""
        res = self.engine.optimize(
            user_query="How does authentication work?",
            ranked_context_items=self.sample_items,
            max_token_budget=100000,
            intent_result=self.intent,
            mode=OptimizationMode.DETERMINISTIC,
            tokenizer=count_tokens_precise,
        )
        measured = count_tokens_precise(res.optimized_context)
        self.assertLessEqual(measured, 100000)
        self.assertEqual(len(res.items_retained), len(self.sample_items))
        self.assertEqual(len(res.items_removed), 0)

    def test_empty_context(self):
        """Empty candidates list must cleanly return empty context and 0 tokens."""
        res = self.engine.optimize(
            user_query="Empty query",
            ranked_context_items=[],
            max_token_budget=1000,
            intent_result=self.intent,
            mode=OptimizationMode.DETERMINISTIC,
            tokenizer=count_tokens_precise,
        )
        self.assertEqual(res.optimized_context, "")
        self.assertEqual(res.estimated_input_tokens, 0)
        self.assertEqual(len(res.items_retained), 0)

    def test_oversized_single_item(self):
        """A single candidate with 2,000 code tokens tested against a 50-token budget must not overflow."""
        long_code = "\n".join([f"    x_{i} = calculate_value({i}) * 3.14159" for i in range(250)])
        huge_item = [
            make_mock_result(
                entity="HeavyModule.massive_computation",
                content=long_code,
                score=0.99,
            )
        ]
        self.assertGreater(count_tokens_precise(long_code), 500)

        res = self.engine.optimize(
            user_query="Explain calculation",
            ranked_context_items=huge_item,
            max_token_budget=50,
            intent_result=self.intent,
            mode=OptimizationMode.DETERMINISTIC,
            tokenizer=count_tokens_precise,
        )
        measured = count_tokens_precise(res.optimized_context)
        self.assertLessEqual(
            measured,
            50,
            f"Oversized item overflowed 50-token budget: measured {measured} tokens"
        )

    def test_multiple_items_knapsack_efficiency(self):
        """Higher utility items must be packed first, respecting the exact budget."""
        budget = 150
        res = self.engine.optimize(
            user_query="Authentication details",
            ranked_context_items=self.sample_items,
            max_token_budget=budget,
            intent_result=self.intent,
            mode=OptimizationMode.DETERMINISTIC,
            tokenizer=count_tokens_precise,
        )
        measured = count_tokens_precise(res.optimized_context)
        self.assertLessEqual(measured, budget)
        self.assertGreater(len(res.items_retained), 0)
        # Highest ranked item should be retained
        retained_entities = [r["entity"] for r in res.items_retained]
        self.assertIn("AuthService.verify_token", retained_entities)

    def test_unicode_and_special_characters(self):
        """Context containing multi-byte UTF-8, Chinese/Japanese comments, and emojis must respect budget."""
        unicode_item = [
            make_mock_result(
                entity="InternationalizedModule.挨拶",
                content="# 日本語のコメント: ユーザー認証処理 🚀\n# 🌟 Special UTF-8 mathematical symbols: ∑(x_i) ∈ ℝ² ⨂ ⨁\ndef greet(name: str) -> str:\n    return f'こんにちは {name} 👋!'",
                score=0.92,
            )
        ]
        for budget in [30, 60, 100]:
            res = self.engine.optimize(
                user_query="挨拶処理",
                ranked_context_items=unicode_item,
                max_token_budget=budget,
                intent_result=self.intent,
                mode=OptimizationMode.DETERMINISTIC,
                tokenizer=count_tokens_precise,
            )
            measured = count_tokens_precise(res.optimized_context)
            self.assertLessEqual(
                measured,
                budget,
                f"Unicode item overflowed budget {budget}: measured {measured} tokens"
            )

    def test_deterministic_repeatability(self):
        """Successive runs with identical inputs must produce bit-for-bit identical output and counts."""
        budget = 200
        run1 = self.engine.optimize(
            user_query="How does authentication work?",
            ranked_context_items=self.sample_items,
            max_token_budget=budget,
            intent_result=self.intent,
            mode=OptimizationMode.DETERMINISTIC,
            tokenizer=count_tokens_precise,
        )
        run2 = self.engine.optimize(
            user_query="How does authentication work?",
            ranked_context_items=self.sample_items,
            max_token_budget=budget,
            intent_result=self.intent,
            mode=OptimizationMode.DETERMINISTIC,
            tokenizer=count_tokens_precise,
        )
        self.assertEqual(run1.optimized_context, run2.optimized_context)
        self.assertEqual(run1.estimated_input_tokens, run2.estimated_input_tokens)
        self.assertEqual(
            count_tokens_precise(run1.optimized_context),
            count_tokens_precise(run2.optimized_context),
        )

    def test_formatting_overhead_accounted(self):
        """Separators between items must be tested so that formatting overhead cannot cause budget overflow."""
        # Packing multiple items
        for budget in [100, 200, 300]:
            res = self.engine.optimize(
                user_query="How does authentication work?",
                ranked_context_items=self.sample_items,
                max_token_budget=budget,
                intent_result=self.intent,
                mode=OptimizationMode.DETERMINISTIC,
                tokenizer=count_tokens_precise,
            )
            # Authoritative tiktoken count of the assembled context
            actual_count = count_tokens_precise(res.optimized_context)
            self.assertLessEqual(
                actual_count,
                budget,
                f"Assembled context with separators exceeded budget {budget}: got {actual_count}"
            )

    def test_regression_sweep_never_exceeds_budget(self):
        """CRITICAL REGRESSION TEST: Across all budget tiers, measured context must NEVER exceed declared budget."""
        budget_sweep = [25, 50, 75, 100, 150, 250, 500, 750, 1000, 1500, 2500, 4000]
        for b in budget_sweep:
            res = self.engine.optimize(
                user_query="How does authentication work with JWT and RSA?",
                ranked_context_items=self.sample_items,
                max_token_budget=b,
                intent_result=self.intent,
                mode=OptimizationMode.DETERMINISTIC,
                tokenizer=count_tokens_precise,
            )
            measured = count_tokens_precise(res.optimized_context)
            self.assertLessEqual(
                measured,
                b,
                f"Regression failure at budget {b}: measured context was {measured} tokens ({measured/b*100:.1f}%)"
            )
            utilization = (measured / b) * 100.0
            self.assertLessEqual(utilization, 100.0)

    def test_raw_greedy_mode_never_exceeds_budget(self):
        """Raw greedy mode (OptimizationMode.NONE) must also strictly respect the budget."""
        for b in [50, 100, 250, 500, 1000, 1500]:
            res = self.engine.optimize(
                user_query="How does authentication work?",
                ranked_context_items=self.sample_items,
                max_token_budget=b,
                intent_result=self.intent,
                mode=OptimizationMode.NONE,
                tokenizer=count_tokens_precise,
            )
            measured = count_tokens_precise(res.optimized_context)
            self.assertLessEqual(
                measured,
                b,
                f"Raw greedy mode overflowed budget {b}: measured {measured} tokens"
            )

    def test_adaptive_context_packer_exact_tokenizer(self):
        """AdaptiveContextPacker with exact tokenizer must never exceed token_budget."""
        packer = AdaptiveContextPacker(tokenizer=count_tokens_precise)
        for b in [50, 100, 250, 500, 1000]:
            ctx, tok = packer.pack(self.sample_items, self.intent, token_budget=b)
            measured = count_tokens_precise(ctx)
            self.assertLessEqual(
                measured,
                b,
                f"AdaptiveContextPacker overflowed budget {b}: measured {measured} tokens"
            )


if __name__ == "__main__":
    unittest.main()
