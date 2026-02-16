# file: oms/sequence.py

from infrastructure.logger import logger

class SequenceValidator:
    """
    单调性守卫
    """
    def __init__(self):
        self.last_seq = 0
        
    def check(self, incoming_seq: int) -> bool:
        """
        验证输入序列号是否严格等于 last + 1
        """
        # 初始化状态 (第一条消息)
        if self.last_seq == 0:
            self.last_seq = incoming_seq
            return True
            
        expected = self.last_seq + 1
        
        if incoming_seq == expected:
            self.last_seq = incoming_seq
            return True
        else:
            logger.critical(f"[Seq] GAP! Expected {expected}, Got {incoming_seq}")
            return False
            
    def reset(self):
        self.last_seq = 0