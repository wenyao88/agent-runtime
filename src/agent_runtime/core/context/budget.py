from dataclasses import dataclass


@dataclass
class TokenBudget:
    model_max_tokens: int = 128000
    reserved_output: int = 4096
    safety_margin: float = 0.9
    compaction_ratio: float = 0.8  # 触发压缩的占用比例（由 Settings 的阈值配置驱动）

    @property
    def available(self) -> int:
        return int((self.model_max_tokens - self.reserved_output) * self.safety_margin)

    @property
    def compaction_threshold(self) -> int:
        return int(self.available * self.compaction_ratio)
