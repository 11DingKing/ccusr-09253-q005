# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 异常学时复核案件

负向修正、超长签到和重叠活动不再直接改变学员总学时，而是统一进入复核案件：

- `POST /api/plans/{plan}/cases/detect` 扫描事件流立案，同一异常由去重键保证不会重复立案；
- 案件状态机为 `detected → claimed → adjudicated`，裁决后可 `reopened` 再认领，合并或拆分后进入 `merged`/`split` 终态且谱系（`lineage`）保留原始来源；
- `POST .../cases/{id}/claim`、`.../evidence`、`.../adjudicate`、`.../reopen` 分别对应认领、补证、裁决与复开，操作人经 `X-Actor-Id` / `X-Actor-Role`（`reviewer` 或 `admin`）头部识别，仅处理人或管理员可办理，合并与拆分仅管理员可操作；
- 裁决确认时关联的最终修正事件（`leave_correction`）自此计入学时，被案件吸收的原始事件始终挂起；复开后原修正事件随即挂起，直至重新裁决；
- 未决案件在实时与冻结快照中以 `open_cases`、`pending_review_seconds`、`held_adjustments` 清晰标注，但不计入已确认学时。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询，以及复核案件的检测、认领、补证、裁决、复开、合并拆分、并发认领、权限隔离与重启恢复；运行过程中不需要单独的数据库或网络服务。
