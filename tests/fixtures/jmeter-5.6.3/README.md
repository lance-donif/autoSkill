# JMeter 5.6.3 report reference

The 22 samples in `samples.csv` are synthetic. They include overlapping requests, two labels, timed bucket boundaries, a failed assertion with HTTP 200, and both default APDEX thresholds. No network request is needed.

Generate the reference using the [official Apache JMeter 5.6.3 archive](https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-5.6.3.tgz) and the adjacent [SHA-512 checksum](https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-5.6.3.tgz.sha512). Run from this fixture directory, supplying a new output directory.

```sh
/path/to/apache-jmeter-5.6.3/bin/jmeter -g samples.csv -j /tmp/jmeter-samples.log -o /tmp/jmeter-samples-report
```

`expected.json` copies the statistics from the generated `statistics.json`, the APDEX and error tables from `content/js/dashboard.js`, and the named graph series from `content/js/graph.js`. Series points are ordered by their x coordinate for comparison. The percentile curve keeps 15 reference points per label. Error rows contain message, count, percentage of errors and percentage of all samples. JMeter's default report granularity is 60,000 ms, and the default satisfied and tolerated thresholds are 500 ms and 1,500 ms.
