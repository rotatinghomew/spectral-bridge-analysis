# 薄膜分光测量资料交换

`data/` 收录供演练使用的合成记录，不含真实企业或人员资料。资料使用 UTF-8 逗号分隔，首行为字段名，带逗号的单值用双引号括起。日期采用 ISO 表示，空字段表示来源未填或不适用，不代表数值零。`sources.json` 记录各表列名、身份键和可直接核对的引用。

## 业务资料

- `instruments.csv`：读头及实际使用区间，读值单位为归一化透过率百分点。
- `slices.csv`：检件与母片归属，互换组以明确记录为准，空值表示未确认。
- `readings.csv`：读头原始读数；同片可有重复读数。以记录时刻 received 作为可见时点，day 为测量日。
- `pair_changes.csv`：互换声明更正。撤销只能使该检件与其他检件之间的互换失效，不取消读数。
- `orders.csv`：R0 尺度验收边界含端点；line_only=1 时只接收产线取样，lab_allowed=1 时可使用同卷复测。
- `certificates.csv`：证书覆盖的卷材，以逗号分隔。group_max_mean_error 是订单允许的组级均值偏移上界；common_reader_bias 标示同读头偏移不能独立抵消。
- `retests.csv`：复测申请按物理片、设备及时段识别；同一卷同规格的订单可引用同一批留样。

所有日期是业务所在地日历日，生效区间为左闭右开。已退回的互换声明不会删去原始读数。空白发运日表示未确认发运；误差上界和读值都在 R0 标度上。

## 本地核对

需要 Python 3.11。分析环境可使用 `python3 -m pip install -r requirements.txt` 安装锁定的数值依赖。

从仓库根目录执行 `python3 check_sources.py`，返回各表行数；其他同格式资料可用 `python3 check_sources.py 路径` 检查。脚本只读资料并报告结构或引用错误，不给出业务结论。执行 `python3 -m unittest discover -s tests -v` 检查资料约定及拒绝重复身份的行为。
