# Job Feed Specification

## Overview

- Provider: Jobg8
- Format: ZIP containing XML
- Update Frequency: Every hour
- Current Size:
  - ZIP: ~170 MB
  - XML: ~1 GB
- Current Jobs: ~350,000
- Future: Expected to grow

## Feed Structure

```
ZIP
 └── jobs.xml
      └── <Jobs>
            ├── <Job>
            ├── <Job>
            └── ...
```

Each `<Job>` represents one job.

## Sample Job

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Jobs>
  <Job>
    <AdvertiserName>ClickNova Jobs</AdvertiserName>
    <AdvertiserType>Agency</AdvertiserType>
    <SenderReference>987654321</SenderReference>
    <DisplayReference>TEST001</DisplayReference>
    <Classification>IT & Communications</Classification>
    <Position><![CDATA[Junior Web Developer]]></Position>
    <Description><![CDATA[
Join our fast-growing tech team as a Junior Web Developer.
Must know HTML, CSS, and basic PHP.
]]></Description>
    <Country>United States</Country>
    <Location>Remote</Location>
    <Area>North America</Area>
    <PostalCode>00000</PostalCode>
    <ApplicationURL>https://www.jobg8.com/Traffic.aspx?test123</ApplicationURL>
    <Language>2057</Language>
    <EmploymentType>Full Time</EmploymentType>
    <StartDate>Immediate</StartDate>
    <Duration>Permanent</Duration>
    <WorkHours>Full Time</WorkHours>
    <SalaryCurrency>USD</SalaryCurrency>
    <SalaryMinimum>3000</SalaryMinimum>
    <SalaryMaximum>4000</SalaryMaximum>
    <SalaryPeriod>Monthly</SalaryPeriod>
    <SalaryAdditional>Health insurance + Stock Options</SalaryAdditional>
    <LogoURL>https://www.jobg8.com/test-logo.png</LogoURL>
    <JobType>TRAFFIC</JobType>
    <SellPrice>0.20</SellPrice>
    <SellPriceCurrency>USD</SellPriceCurrency>
    <RevenueType>CPC</RevenueType>
  </Job>
</Jobs>
```

## Important Notes

- `SenderReference` is unique for every job.
- Provider may:
  - Add jobs
  - Update jobs
  - Remove jobs
- Feed represents the latest snapshot.
- Missing jobs in a new feed should be marked inactive.
- Salary is optional.
- Logo is optional.
- Description can be very large.
- One XML contains all jobs.

## Statistics

- Jobs: ~350,000
- Average description: ~2,100 characters
- Max description: ~10,000 characters
- Salary available: ~13.6%
- Unique logos: ~234
- Duplicate SenderReference: 0

## Development Rules

- Never assume a field exists if it is not in the feed.
- Preserve unknown fields in the raw payload if needed.
- The feed is the source of truth.
- Database design should follow the feed, not the other way around.