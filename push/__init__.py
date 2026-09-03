# -*- coding: utf-8 -*-
"""股票推送系统（独立模块）

设计目标与约束见 docs/push-system-design.md。要点：
  - 独立架构：只 import 复用现有代码（买入战法 / BarContext / DataSource /
    Scanner._live_daily_bar 等），不改动 app/* 与 config.py 任何既有文件。
  - 运行模型：常驻的只有一个轻量调度器；实际干活的两个作业（14:00 盘中扫描推送、
    盘后数据更新）都是"用完即弃"——到点实例化、干完销毁、状态只落磁盘不落内存。
  - 只推送命中买入信号的股票，按 K 线级别合并成一条消息；当前只实现日 K，30m 预留。

本包 P1 阶段只包含与外部渠道/配置/日志相关的骨架，不依赖 app/* 与 kline.db，
因此 `python -m push --test-push` 在没有行情库的机器上也能跑通。
"""

__version__ = "0.1.0"
