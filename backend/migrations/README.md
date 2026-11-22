# 初始 schema

`src.models.Base.metadata.create_all()` 按外键顺序建表，由 `src.main` 在 API 首次启动时执行。已有开发卷不执行自动字段迁移。项目 README 记录了模型与首次建库入口。

## 批次与暂停字段升级

批次闸门与自动放行暂停引入了 `delay_incidents.berth_code/cycle_no/seq_no/batch_no/in_margin_band/auto_released_*`、`berths.cycle_no/next_seq` 以及新表 `berth_suspensions`。`src.schema_patch.apply()` 在每次启动建表后运行，通过 `information_schema` 判断列/表是否存在，缺失才执行 `ALTER`/`CREATE`，并把存量案件按泊位与观测时间回填为第一轮的连续序号、十案一批；全新数据库由 `create_all` 直接建出完整结构，补丁全部跳过。该补丁面向本地演示卷，生产变更仍应使用正式迁移工具。
