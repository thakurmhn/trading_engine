# ============================================================================
#  pivot_reaction_engine.py — Mandatory Pivot Evaluation & Entry Validation
# ============================================================================
"""
SYSTEM ROLE: Core Decision Module

The Pivot Reaction Engine is a MANDATORY stage in the trading decision pipeline.
No trade signal may execute without pivot evaluation and validation.

RESPONSIBILITIES:
1. Evaluate price interaction with ALL pivot levels on every candle close
2. Classify interaction type (touch, rejection, acceptance, breakout, etc.)
3. Detect pivot clusters (multiple pivots in close proximity)
4. Validate trade signals against pivot context
5. Block trades that ignore pivot reactions
6. Maintain pivot integrity metrics

OPERATING PRINCIPLE:
Pivots define market structure. All trading decisions must respect this structure.
"""

import logging
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from enum import Enum

# ============================================================================
# INTERACTION TYPES
# ============================================================================

class PivotInteractionType(Enum):
    """Classification of price interaction with pivot level"""
    NO_INTERACTION = "NO_INTERACTION"
    PIVOT_TOUCH = "PIVOT_TOUCH"
    PIVOT_REJECTION = "PIVOT_REJECTION"
    PIVOT_ACCEPTANCE = "PIVOT_ACCEPTANCE"
    PIVOT_BREAKOUT = "PIVOT_BREAKOUT"
    PIVOT_BREAKDOWN = "PIVOT_BREAKDOWN"
    FAILED_BREAKOUT = "FAILED_BREAKOUT"
    FAILED_BREAKDOWN = "FAILED_BREAKDOWN"
    CLUSTER_REJECTION = "CLUSTER_REJECTION"
    CLUSTER_BREAKOUT = "CLUSTER_BREAKOUT"


class PivotReaction(Enum):
    """Pivot acceptance vs rejection classification"""
    ACCEPTANCE = "ACCEPTANCE"
    REJECTION = "REJECTION"
    NEUTRAL = "NEUTRAL"


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class PivotLevel:
    """Single pivot level definition"""
    family: str          # CPR, TRADITIONAL, CAMARILLA
    level: str           # R1, S2, TC, etc.
    price: float
    type: str            # RESISTANCE, SUPPORT, NEUTRAL


@dataclass
class PivotInteraction:
    """Result of evaluating candle interaction with a pivot"""
    pivot: PivotLevel
    interaction_type: PivotInteractionType
    reaction: PivotReaction
    distance: float      # Distance from pivot in points
    used_in_decision: bool = False
    
    def __str__(self):
        return (f"{self.pivot.family}_{self.pivot.level}@{self.pivot.price:.2f} "
                f"{self.interaction_type.value} {self.reaction.value}")


@dataclass
class PivotCluster:
    """Group of nearby pivot levels"""
    cluster_id: int
    pivots: List[PivotLevel]
    center_price: float
    range_pts: float
    dominant_family: str
    interaction_type: Optional[PivotInteractionType] = None


@dataclass
class CandleData:
    """Candle OHLC data"""
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    atr: float = 10.0  # Default ATR if not available


# ============================================================================
# PIVOT REACTION ENGINE
# ============================================================================

class PivotReactionEngine:
    """
    Mandatory decision module for pivot evaluation and entry validation.
    
    This engine sits between signal detection and trade execution.
    No trade may proceed without pivot validation.
    """
    
    def __init__(self, atr_multiplier: float = 0.05):
        """
        Initialize Pivot Reaction Engine.
        
        Args:
            atr_multiplier: Tolerance for pivot touch detection (% of ATR)
        """
        self.atr_multiplier = atr_multiplier
        self.cluster_threshold_atr = 0.5  # Pivots within 0.5 ATR = cluster
        
        # Metrics
        self.metrics = {
            "total_candles_evaluated": 0,
            "pivot_levels_checked": 0,
            "pivot_interactions_detected": 0,
            "pivot_rejections": 0,
            "pivot_acceptances": 0,
            "pivot_breakouts": 0,
            "pivot_breakdowns": 0,
            "pivot_cluster_events": 0,
            "trades_with_pivot_confirmation": 0,
            "trades_blocked_no_pivot": 0,
        }
        
        # State tracking
        self.last_candle_interactions: Dict[str, PivotInteraction] = {}
        self.pivot_clusters: List[PivotCluster] = []
        self.previous_candle: Optional[CandleData] = None
        
        logging.info("[PIVOT_ENGINE] Initialized - Mandatory pivot validation ACTIVE")
    
    # ========================================================================
    # CORE EVALUATION PIPELINE
    # ========================================================================
    
    def evaluate_candle(self, candle: CandleData, pivot_levels: Dict[str, Dict[str, float]]) -> Dict[str, PivotInteraction]:
        """
        MANDATORY: Evaluate candle interaction with ALL pivot levels.
        
        This function MUST be called on every candle close before any
        trade signal is allowed to proceed.
        
        Args:
            candle: Current candle OHLC data
            pivot_levels: Dict of pivot families and their levels
                         {"CPR": {"TC": 25471, "P": 25457, ...}, ...}
        
        Returns:
            Dict mapping pivot_key to PivotInteraction
        """
        self.metrics["total_candles_evaluated"] += 1
        interactions = {}
        
        # Convert pivot_levels dict to PivotLevel objects
        all_pivots = self._parse_pivot_levels(pivot_levels)
        
        # Detect pivot clusters FIRST (before individual evaluation)
        self.pivot_clusters = self._detect_pivot_clusters(all_pivots, candle.atr)
        
        # Evaluate each pivot level
        for pivot in all_pivots:
            self.metrics["pivot_levels_checked"] += 1
            
            interaction = self._classify_interaction(candle, pivot)
            pivot_key = f"{pivot.family}_{pivot.level}"
            interactions[pivot_key] = interaction
            
            # Update metrics
            if interaction.interaction_type != PivotInteractionType.NO_INTERACTION:
                self.metrics["pivot_interactions_detected"] += 1
                
                if interaction.reaction == PivotReaction.REJECTION:
                    self.metrics["pivot_rejections"] += 1
                elif interaction.reaction == PivotReaction.ACCEPTANCE:
                    self.metrics["pivot_acceptances"] += 1
                
                if interaction.interaction_type == PivotInteractionType.PIVOT_BREAKOUT:
                    self.metrics["pivot_breakouts"] += 1
                elif interaction.interaction_type == PivotInteractionType.PIVOT_BREAKDOWN:
                    self.metrics["pivot_breakdowns"] += 1
            
            # Mandatory audit log
            self._log_pivot_interaction(candle, interaction)
        
        # Evaluate cluster interactions
        self._evaluate_cluster_interactions(candle, interactions)
        
        # Store for next candle comparison
        self.last_candle_interactions = interactions
        self.previous_candle = candle
        
        # Validate family coverage
        self._validate_family_coverage(interactions)
        
        return interactions
    
    def validate_trade_signal(self, signal_side: str, signal_reason: str, 
                             interactions: Dict[str, PivotInteraction]) -> Tuple[bool, str]:
        """
        MANDATORY: Validate trade signal against pivot context.
        
        This function MUST be called before executing any trade.
        If validation fails, the trade MUST be blocked.
        
        Args:
            signal_side: "CALL" or "PUT"
            signal_reason: Reason string from signal detection
            interactions: Pivot interactions from evaluate_candle()
        
        Returns:
            (is_valid, reason) - True if trade allowed, False if blocked
        """
        # Check 1: Pivot evaluation completed?
        if not interactions:
            self.metrics["trades_blocked_no_pivot"] += 1
            logging.error(
                "[PIVOT_ENGINE][BLOCK] Trade signal without pivot evaluation - "
                "PIVOT_DECISION_PIPELINE_FAILURE"
            )
            return False, "PIVOT_EVALUATION_MISSING"
        
        # Check 2: Does signal reference pivot context?
        pivot_keywords = [
            "PIVOT", "REJECTION", "ACCEPTANCE", "BREAKOUT", "BREAKDOWN",
            "SUPPORT", "RESISTANCE", "CPR", "CAMARILLA", "TRADITIONAL"
        ]
        has_pivot_context = any(kw in signal_reason.upper() for kw in pivot_keywords)
        
        # Check 3: Find relevant pivot interactions for this signal
        relevant_interactions = self._find_relevant_interactions(
            signal_side, interactions
        )
        
        if not relevant_interactions:
            # No pivot interaction detected - allow but warn
            logging.warning(
                f"[PIVOT_ENGINE][WARN] {signal_side} signal without pivot interaction - "
                f"reason={signal_reason}"
            )
            # Don't block, but mark as low confidence
            return True, "NO_PIVOT_INTERACTION"
        
        # Check 4: Validate signal direction aligns with pivot reaction
        is_aligned, alignment_reason = self._validate_signal_alignment(
            signal_side, relevant_interactions
        )
        
        if not is_aligned:
            self.metrics["trades_blocked_no_pivot"] += 1
            logging.warning(
                f"[PIVOT_ENGINE][BLOCK] {signal_side} signal conflicts with pivot reaction - "
                f"{alignment_reason}"
            )
            return False, alignment_reason
        
        # Signal validated - mark interactions as used
        for interaction in relevant_interactions:
            interaction.used_in_decision = True
        
        self.metrics["trades_with_pivot_confirmation"] += 1
        logging.info(
            f"[PIVOT_ENGINE][VALIDATED] {signal_side} signal confirmed by pivot context - "
            f"{alignment_reason}"
        )
        
        return True, alignment_reason
    
    # ========================================================================
    # INTERACTION CLASSIFICATION
    # ========================================================================
    
    def _classify_interaction(self, candle: CandleData, pivot: PivotLevel) -> PivotInteraction:
        """
        Classify how candle interacted with pivot level.
        
        Logic:
        1. Touch: High/low within tolerance of pivot
        2. Rejection: Touch + close moves away from pivot
        3. Acceptance: Close beyond pivot + momentum continues
        4. Breakout: Close above resistance pivot
        5. Breakdown: Close below support pivot
        6. Failed breakout/breakdown: Break then immediate reversal
        """
        o, h, l, c = candle.open, candle.high, candle.low, candle.close
        pivot_price = pivot.price
        tolerance = candle.atr * self.atr_multiplier
        distance = c - pivot_price
        
        # Default: No interaction
        interaction_type = PivotInteractionType.NO_INTERACTION
        reaction = PivotReaction.NEUTRAL
        
        # Touch detection
        touched_high = abs(h - pivot_price) <= tolerance
        touched_low = abs(l - pivot_price) <= tolerance
        touched = touched_high or touched_low
        
        if touched:
            interaction_type = PivotInteractionType.PIVOT_TOUCH
            
            # Rejection detection (touch + close moves away)
            if pivot.type == "RESISTANCE":
                if touched_high and c < pivot_price - tolerance:
                    interaction_type = PivotInteractionType.PIVOT_REJECTION
                    reaction = PivotReaction.REJECTION
            elif pivot.type == "SUPPORT":
                if touched_low and c > pivot_price + tolerance:
                    interaction_type = PivotInteractionType.PIVOT_REJECTION
                    reaction = PivotReaction.REJECTION
        
        # Breakout detection (close above resistance)
        if pivot.type == "RESISTANCE" and c > pivot_price + tolerance:
            interaction_type = PivotInteractionType.PIVOT_BREAKOUT
            reaction = PivotReaction.ACCEPTANCE
            
            # Failed breakout check (previous candle broke, this candle reversed)
            if self.previous_candle and self.previous_candle.close > pivot_price:
                if c < pivot_price:
                    interaction_type = PivotInteractionType.FAILED_BREAKOUT
                    reaction = PivotReaction.REJECTION
        
        # Breakdown detection (close below support)
        if pivot.type == "SUPPORT" and c < pivot_price - tolerance:
            interaction_type = PivotInteractionType.PIVOT_BREAKDOWN
            reaction = PivotReaction.ACCEPTANCE
            
            # Failed breakdown check
            if self.previous_candle and self.previous_candle.close < pivot_price:
                if c > pivot_price:
                    interaction_type = PivotInteractionType.FAILED_BREAKDOWN
                    reaction = PivotReaction.REJECTION
        
        # Acceptance detection (close beyond pivot, momentum continues)
        if interaction_type == PivotInteractionType.NO_INTERACTION:
            if pivot.type == "RESISTANCE" and c > pivot_price + tolerance:
                # Check if previous candle also closed above
                if self.previous_candle and self.previous_candle.close > pivot_price:
                    interaction_type = PivotInteractionType.PIVOT_ACCEPTANCE
                    reaction = PivotReaction.ACCEPTANCE
            elif pivot.type == "SUPPORT" and c < pivot_price - tolerance:
                if self.previous_candle and self.previous_candle.close < pivot_price:
                    interaction_type = PivotInteractionType.PIVOT_ACCEPTANCE
                    reaction = PivotReaction.ACCEPTANCE
        
        return PivotInteraction(
            pivot=pivot,
            interaction_type=interaction_type,
            reaction=reaction,
            distance=distance,
            used_in_decision=False
        )
    
    # ========================================================================
    # CLUSTER DETECTION
    # ========================================================================
    
    def _detect_pivot_clusters(self, pivots: List[PivotLevel], atr: float) -> List[PivotCluster]:
        """
        Detect clusters of nearby pivot levels.
        
        Pivots within cluster_threshold_atr * ATR are grouped as a cluster.
        Clusters represent high-probability reaction zones.
        """
        if not pivots:
            return []
        
        cluster_threshold = atr * self.cluster_threshold_atr
        
        # Sort pivots by price
        sorted_pivots = sorted(pivots, key=lambda p: p.price)
        
        clusters = []
        current_cluster = [sorted_pivots[0]]
        
        for i in range(1, len(sorted_pivots)):
            price_diff = sorted_pivots[i].price - current_cluster[-1].price
            
            if price_diff <= cluster_threshold:
                current_cluster.append(sorted_pivots[i])
            else:
                # Save cluster if it has multiple pivots
                if len(current_cluster) > 1:
                    clusters.append(self._create_cluster(len(clusters), current_cluster))
                current_cluster = [sorted_pivots[i]]
        
        # Don't forget last cluster
        if len(current_cluster) > 1:
            clusters.append(self._create_cluster(len(clusters), current_cluster))
        
        # Log clusters
        for cluster in clusters:
            self.metrics["pivot_cluster_events"] += 1
            pivot_names = [f"{p.family}_{p.level}" for p in cluster.pivots]
            logging.info(
                f"[PIVOT_CLUSTER_EVENT] cluster_id={cluster.cluster_id} "
                f"pivots={pivot_names} center={cluster.center_price:.2f} "
                f"range={cluster.range_pts:.2f}pts count={len(cluster.pivots)} "
                f"dominant={cluster.dominant_family}"
            )
        
        return clusters
    
    def _create_cluster(self, cluster_id: int, pivots: List[PivotLevel]) -> PivotCluster:
        """Create PivotCluster from list of nearby pivots"""
        center_price = sum(p.price for p in pivots) / len(pivots)
        range_pts = pivots[-1].price - pivots[0].price
        
        # Determine dominant family (most pivots in cluster)
        family_counts = {}
        for p in pivots:
            family_counts[p.family] = family_counts.get(p.family, 0) + 1
        dominant_family = max(family_counts, key=family_counts.get)
        
        return PivotCluster(
            cluster_id=cluster_id,
            pivots=pivots,
            center_price=center_price,
            range_pts=range_pts,
            dominant_family=dominant_family
        )
    
    def _evaluate_cluster_interactions(self, candle: CandleData, 
                                      interactions: Dict[str, PivotInteraction]):
        """
        Evaluate candle interaction with pivot clusters.
        
        Cluster interactions receive higher decision weight.
        """
        for cluster in self.pivot_clusters:
            # Check if candle interacted with cluster
            tolerance = candle.atr * self.atr_multiplier
            cluster_touched = (
                candle.low <= cluster.center_price + cluster.range_pts / 2 + tolerance and
                candle.high >= cluster.center_price - cluster.range_pts / 2 - tolerance
            )
            
            if cluster_touched:
                # Determine cluster interaction type
                if candle.close > cluster.center_price + tolerance:
                    cluster.interaction_type = PivotInteractionType.CLUSTER_BREAKOUT
                    logging.info(
                        f"[PIVOT_CLUSTER_BREAKOUT] cluster_id={cluster.cluster_id} "
                        f"center={cluster.center_price:.2f} close={candle.close:.2f}"
                    )
                elif candle.close < cluster.center_price - tolerance:
                    cluster.interaction_type = PivotInteractionType.CLUSTER_REJECTION
                    logging.info(
                        f"[PIVOT_CLUSTER_REJECTION] cluster_id={cluster.cluster_id} "
                        f"center={cluster.center_price:.2f} close={candle.close:.2f}"
                    )
    
    # ========================================================================
    # SIGNAL VALIDATION
    # ========================================================================
    
    def _find_relevant_interactions(self, signal_side: str, 
                                   interactions: Dict[str, PivotInteraction]) -> List[PivotInteraction]:
        """
        Find pivot interactions relevant to this signal side.
        
        CALL signals: Look for support holds, resistance breakouts
        PUT signals: Look for resistance rejections, support breakdowns
        """
        relevant = []
        
        for interaction in interactions.values():
            if interaction.interaction_type == PivotInteractionType.NO_INTERACTION:
                continue
            
            pivot_type = interaction.pivot.type
            interaction_type = interaction.interaction_type
            
            # CALL signal relevance
            if signal_side == "CALL":
                if pivot_type == "SUPPORT" and interaction_type in [
                    PivotInteractionType.PIVOT_REJECTION,
                    PivotInteractionType.PIVOT_ACCEPTANCE
                ]:
                    relevant.append(interaction)
                elif pivot_type == "RESISTANCE" and interaction_type in [
                    PivotInteractionType.PIVOT_BREAKOUT,
                    PivotInteractionType.PIVOT_ACCEPTANCE
                ]:
                    relevant.append(interaction)
            
            # PUT signal relevance
            elif signal_side == "PUT":
                if pivot_type == "RESISTANCE" and interaction_type in [
                    PivotInteractionType.PIVOT_REJECTION,
                    PivotInteractionType.PIVOT_ACCEPTANCE
                ]:
                    relevant.append(interaction)
                elif pivot_type == "SUPPORT" and interaction_type in [
                    PivotInteractionType.PIVOT_BREAKDOWN,
                    PivotInteractionType.PIVOT_ACCEPTANCE
                ]:
                    relevant.append(interaction)
        
        return relevant
    
    def _validate_signal_alignment(self, signal_side: str, 
                                  interactions: List[PivotInteraction]) -> Tuple[bool, str]:
        """
        Validate that signal direction aligns with pivot reactions.
        
        Returns:
            (is_aligned, reason)
        """
        if not interactions:
            return True, "NO_PIVOT_INTERACTION"
        
        # Check for conflicting signals
        rejections = [i for i in interactions if i.reaction == PivotReaction.REJECTION]
        acceptances = [i for i in interactions if i.reaction == PivotReaction.ACCEPTANCE]
        
        if signal_side == "CALL":
            # CALL signal should align with:
            # - Support rejections (bounce)
            # - Resistance breakouts (acceptance above)
            
            # Check for conflicting resistance rejections
            resistance_rejections = [
                i for i in rejections 
                if i.pivot.type == "RESISTANCE"
            ]
            if resistance_rejections:
                pivot_str = resistance_rejections[0].pivot.level
                return False, f"CALL_CONFLICTS_WITH_RESISTANCE_REJECTION_{pivot_str}"
            
            # Check for positive confirmations
            support_rejections = [
                i for i in rejections 
                if i.pivot.type == "SUPPORT"
            ]
            resistance_breakouts = [
                i for i in acceptances 
                if i.pivot.type == "RESISTANCE"
            ]
            
            if support_rejections:
                pivot_str = support_rejections[0].pivot.level
                return True, f"CALL_CONFIRMED_SUPPORT_REJECTION_{pivot_str}"
            if resistance_breakouts:
                pivot_str = resistance_breakouts[0].pivot.level
                return True, f"CALL_CONFIRMED_RESISTANCE_BREAKOUT_{pivot_str}"
        
        elif signal_side == "PUT":
            # PUT signal should align with:
            # - Resistance rejections (rejection)
            # - Support breakdowns (acceptance below)
            
            # Check for conflicting support rejections
            support_rejections = [
                i for i in rejections 
                if i.pivot.type == "SUPPORT"
            ]
            if support_rejections:
                pivot_str = support_rejections[0].pivot.level
                return False, f"PUT_CONFLICTS_WITH_SUPPORT_REJECTION_{pivot_str}"
            
            # Check for positive confirmations
            resistance_rejections = [
                i for i in rejections 
                if i.pivot.type == "RESISTANCE"
            ]
            support_breakdowns = [
                i for i in acceptances 
                if i.pivot.type == "SUPPORT"
            ]
            
            if resistance_rejections:
                pivot_str = resistance_rejections[0].pivot.level
                return True, f"PUT_CONFIRMED_RESISTANCE_REJECTION_{pivot_str}"
            if support_breakdowns:
                pivot_str = support_breakdowns[0].pivot.level
                return True, f"PUT_CONFIRMED_SUPPORT_BREAKDOWN_{pivot_str}"
        
        # No strong confirmation or conflict - allow but neutral
        return True, "PIVOT_NEUTRAL"
    
    # ========================================================================
    # UTILITIES
    # ========================================================================
    
    def _parse_pivot_levels(self, pivot_levels: Dict[str, Dict[str, float]]) -> List[PivotLevel]:
        """Convert pivot_levels dict to list of PivotLevel objects"""
        pivots = []
        
        for family, levels in pivot_levels.items():
            for level_name, level_price in levels.items():
                # Determine pivot type (resistance, support, neutral)
                if level_name.startswith("R") or level_name == "TC":
                    pivot_type = "RESISTANCE"
                elif level_name.startswith("S") or level_name == "BC":
                    pivot_type = "SUPPORT"
                else:
                    pivot_type = "NEUTRAL"
                
                pivots.append(PivotLevel(
                    family=family,
                    level=level_name,
                    price=level_price,
                    type=pivot_type
                ))
        
        return pivots
    
    def _validate_family_coverage(self, interactions: Dict[str, PivotInteraction]):
        """Verify all pivot families were evaluated"""
        required_families = ["CPR", "TRADITIONAL", "CAMARILLA"]
        evaluated_families = set()
        
        for pivot_key in interactions.keys():
            family = pivot_key.split("_")[0]
            evaluated_families.add(family)
        
        missing_families = set(required_families) - evaluated_families
        
        if missing_families:
            logging.error(
                f"[PIVOT_FAMILY_CHECK_FAILED] missing_families={list(missing_families)} "
                "PIVOT_DECISION_PIPELINE_FAILURE"
            )
        else:
            logging.debug(
                "[PIVOT_FAMILY_CHECK] CPR=EVALUATED TRADITIONAL=EVALUATED CAMARILLA=EVALUATED"
            )
    
    def _log_pivot_interaction(self, candle: CandleData, interaction: PivotInteraction):
        """Mandatory audit log for pivot interaction"""
        logging.info(
            f"[PIVOT_AUDIT] timestamp={candle.timestamp} "
            f"candle_close={candle.close:.2f} "
            f"pivot_family={interaction.pivot.family} "
            f"pivot_level={interaction.pivot.level} "
            f"pivot_price={interaction.pivot.price:.2f} "
            f"interaction_type={interaction.interaction_type.value} "
            f"reaction={interaction.reaction.value} "
            f"distance={interaction.distance:.2f}pts "
            f"used_in_decision={interaction.used_in_decision}"
        )
    
    def get_metrics(self) -> Dict[str, int]:
        """Return current pivot integrity metrics"""
        return self.metrics.copy()
    
    def get_metrics_summary(self) -> str:
        """Return formatted metrics summary for dashboard"""
        m = self.metrics
        return f"""
PIVOT REACTION ENGINE METRICS
────────────────────────────────────────
Total Candles Evaluated    : {m['total_candles_evaluated']}
Pivot Levels Checked       : {m['pivot_levels_checked']}
Pivot Interactions Detected: {m['pivot_interactions_detected']}
  - Rejections             : {m['pivot_rejections']}
  - Acceptances            : {m['pivot_acceptances']}
  - Breakouts              : {m['pivot_breakouts']}
  - Breakdowns             : {m['pivot_breakdowns']}
Pivot Cluster Events       : {m['pivot_cluster_events']}
Trades with Pivot Confirm  : {m['trades_with_pivot_confirmation']}
Trades Blocked (No Pivot)  : {m['trades_blocked_no_pivot']}
"""


# ============================================================================
# GLOBAL INSTANCE
# ============================================================================

# Singleton instance - initialized in main.py
pivot_engine: Optional[PivotReactionEngine] = None


def initialize_pivot_engine(atr_multiplier: float = 0.05) -> PivotReactionEngine:
    """Initialize global pivot engine instance"""
    global pivot_engine
    pivot_engine = PivotReactionEngine(atr_multiplier=atr_multiplier)
    return pivot_engine


def get_pivot_engine() -> PivotReactionEngine:
    """Get global pivot engine instance"""
    if pivot_engine is None:
        raise RuntimeError(
            "Pivot Reaction Engine not initialized. "
            "Call initialize_pivot_engine() in main.py startup."
        )
    return pivot_engine
