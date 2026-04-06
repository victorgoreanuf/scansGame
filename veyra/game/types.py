from dataclasses import dataclass, field


@dataclass
class Monster:
    id: str
    name: str
    current_hp: int = 0
    your_dmg: int = 0
    image: str = ""
    joined: bool = False


@dataclass
class MonsterGroup:
    name: str
    count: int = 0
    ids: list[str] = field(default_factory=list)
    total_hp: int = 0
    max_hp: int = 0
    image: str = ""
    instances: list[Monster] = field(default_factory=list)
    total_your_dmg: int = 0
    avg_hp: int = 0
    joined_count: int = 0
    new_count: int = 0


@dataclass
class AttackResult:
    status: str  # success|dead|stamina|rate_limited|error
    damage: int = 0
    monster_hp: int = -1
    message: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        return self.status == "success"

    @property
    def is_dead(self) -> bool:
        return self.status == "dead" or (self.monster_hp == 0 and self.is_success)

    @property
    def is_stamina_exhausted(self) -> bool:
        return self.status == "stamina"

    @property
    def is_rate_limited(self) -> bool:
        return self.status == "rate_limited"


@dataclass
class StaminaOption:
    label: str
    cost: int
    skill_id: str


@dataclass
class TargetConfig:
    name: str
    wave: int
    damage_goal: int = 0
    stamina: str = "10 Stamina"
    priority: int = 1
    ids: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class PlayerStats:
    level: int = 0
    exp_current: int = 0
    exp_max: int = 0
    stamina_current: int = 0
    stamina_max: int = 0

    @property
    def exp_needed(self) -> int:
        return max(0, self.exp_max - self.exp_current)


@dataclass
class DeadMonster:
    """A killed monster available for looting."""
    id: str
    name: str
    your_dmg: int = 0
    exp_per_dmg: float = 0.0

    @property
    def estimated_exp(self) -> float:
        return self.your_dmg * self.exp_per_dmg


@dataclass
class StaminaPotion:
    """A stamina potion from inventory."""
    inv_id: str
    item_type: str
    name: str
    quantity: int
    desc: str = ""
    stamina_value: int = 0  # 0 means full refill

    @property
    def is_full(self) -> bool:
        return self.stamina_value == 0


@dataclass
class CharacterStats:
    """Current character stat values + unspent points from stats.php."""
    unspent: int = 0
    attack: int = 0
    defense: int = 0
    stamina: int = 0


@dataclass
class FarmStats:
    killed: int = 0
    damage: int = 0
    stamina_spent: int = 0
    monsters_attacked: int = 0
    rounds: int = 0
    started_at: float = 0.0
    looted: int = 0
    exp_gained: float = 0.0
