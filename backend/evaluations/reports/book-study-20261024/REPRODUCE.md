# 独立证据包复现

`evidence.zip`保存实验时的准确代码、20条任务、快照、120次真实模型执行记录、重新规划消融结果、统计和原始600次执行的证据包。`evidence-sha256.txt`用于校验压缩包。`environment.json`与`requirements-lock.txt`记录实际运行环境。

在独立目录解压证据包，再把其中`historical-evidence.zip`解压到`historical`目录。以下命令在证据包的`code`目录执行，使用Python 3.11环境：

```powershell
python -m pip install -r ../requirements-lock.txt
python -m scripts.book_evaluation finish ../trial
python -m scripts.book_evaluation analyze ../historical --output ../repeated-analysis
```

这两个命令使用已录制的真实模型响应，无需模型密钥。`finish`核验120次执行的行程、答复和评分；`analyze`重新计算100条任务的历史重复成绩，并核验C记录的调用前缀消融。模型响应缺失的记录单独报告无法验证。

重新执行真实模型实验需要配置现有模型凭据，并使用新的目录：

```powershell
python -m scripts.book_evaluation run ../historical --output ../new-trial
```

固定快照控制外部数据变化。真实模型服务的后续响应可能变化，新的在线成绩应当单独保存。代码版本检验使用全部应用、评测和Python脚本的精确内容；使用证据包中的`code`目录可以保持本次版本。
