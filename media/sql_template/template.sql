Use BatchUpdate
go
----Fetch Data from .9
Exec spImportTable '[446_13042026_gtb]'
go
Select top 10 * from [446_13042026_gtb]
go
----Cleaning
Update [446_13042026_gtb] Set Accountno=Ltrim(Rtrim(AccountNO))
go
Update [446_13042026_gtb] Set AccountStatusCode='Writtenoff' where AccountStatusCode in('WRITE OFF','write0off','WRITEOFF')
go
Update [446_13042026_gtb] Set AccountStatusCode='Performing' where AccountStatusCode='performing'
go
Update [446_13042026_gtb] Set AccountStatusCode='Performing' where AccountStatusCode='Perfroming'
go
Update [446_13042026_gtb] Set AccountStatusCode='Open' where AccountStatusCode='open'
go
Update [446_13042026_gtb] Set AccountStatusCode='Closed' where AccountStatusCode in('Closed','Cloed','Close')
go
Update [446_13042026_gtb] set LoanClassification='Performing' where LoanClassification in ('001','Performing','PERFORMING LOANS','Performimg')
go
Update [446_13042026_gtb] set LoanClassification='Writtenoff' where LoanClassification in ('WRITE OFF','Written Off','WRITEOFF')
go
Update [446_13042026_gtb] set LoanClassification=null where LoanClassification='NULL'
go
Update [446_13042026_gtb] set LoanClassification='Lost' where LoanClassification in('L','Lost')
go
Update [446_13042026_gtb] set LoanClassification='Sub standard' where LoanClassification in ('Sub standard','Substandard')
go
Update [446_13042026_gtb] set LoanClassification='Watchlist' where LoanClassification in ('002','Watchlist','PASS AND WATCH','Watchlisted')
go
Update [446_13042026_gtb] Set AccountStatusCode='Closed',LoanClassification='Performing' where LoanClassification in('Closed','Cloed')
go


Select Monthsinarrears from [446_13042026_gtb] where Monthsinarrears like '%.%'

Select AccountStatusCode,Count(*) from [446_13042026_gtb]
Group by AccountStatusCode
order by AccountStatusCode
go
Select LoanClassification,Count(*) from [446_13042026_gtb]
Group by LoanClassification
order by LoanClassification
go
Alter Table [446_13042026_gtb] Add InValidRec tinyint
go
--Validation
Update [446_13042026_gtb] Set InValidRec=1 where IsNull(AccountNo,'')=''
go
Update [446_13042026_gtb] Set InValidRec=2 where IsNull(AccountNo,'')='00000000'
go
Update [446_13042026_gtb] Set InValidRec=3 where AccountNo like '%e+%'
go
Select AccountNo,Count(*) From [446_13042026_gtb] where InValidRec is null
Group by AccountNo having COunt(*)>1 order by 2 desc
go
----Check Different Status for Same Account
Select AccountNo,Count(*) from
(Select AccountNo,AccountStatusCode From [446_13042026_gtb] where InValidRec is null
Group by AccountNo,AccountStatusCode having COunt(*)>1)a Group by AccountNo
having Count(*)>1 order by 2 desc
go
Create Index [IX_2] on [446_13042026_gtb] (AccountNo)
go
Alter Table [446_13042026_gtb] Add IsDupRecord TinyInt, MasterID Int, UniqueID Bigint Identity(1,1) Primary Key
go
----Exclude Duplicate Records
Update a Set a.InValidRec=9, a.IsDupRecord=1, a.MasterID=b.MinUniqueID
from [446_13042026_gtb] a inner join
(Select AccountNo,Min(UniqueID)MinUniqueID From [446_13042026_gtb] where InValidRec is null
Group by AccountNo having Count(*)>1) b on a.AccountNo=b.AccountNo
and a.UniqueID>b.MinUniqueID and a.InValidRec is null
go
Select InValidRec,Count(*) From [446_13042026_gtb]
Group by InValidRec order by InValidRec
go
Select * Into [446_13042026_gtbV1] From [446_13042026_gtb] where InValidRec is null
go
Select InValidRec,Count(*) From [446_13042026_gtbV1]
Group by InValidRec order by InValidRec
go
Create Index [IX_1] on [446_13042026_gtbV1] (AccountNo)
go
--Matching
----Please change SubScriberID
Select a.*,b.AccountStatusCode as DBAccountStatusCode,b.AccountStatusDate as DBAccountStatusDate,
b.LoanClassification as DBLoanClassification,b.ConsumerID,b.ConsumerAccountID
into [446_13042026_gtbV2]
From [446_13042026_gtbV1] a inner Join
XDSNigeriaConsumer..ConsumerAccount b (NoLock) on
SUBSTRING (Ltrim(Rtrim(a.AccountNo)), PATINDEX('%[^0 ]%', Ltrim(Rtrim(a.AccountNo)) + ' '), LEN(Ltrim(Rtrim(a.AccountNo))))=
SUBSTRING (Ltrim(Rtrim(b.AccountNo)), PATINDEX('%[^0 ]%', Ltrim(Rtrim(b.AccountNo)) + ' '), LEN(Ltrim(Rtrim(b.AccountNo)))) collate Latin1_General_CI_AS
and b.SubscriberID=446 and b.StatusInd='A'
go
Alter Table [446_13042026_gtbV1] Add DbMatch Tinyint
go
Select LoanClassification,Count(*)NumOfAccounts from [446_13042026_gtbV2]
Group by LoanClassification
Order by LoanClassification
go
Select AccountStatusCode,Count(*)NumOfAccounts from [446_13042026_gtbV2]
Group by AccountStatusCode
Order by AccountStatusCode
go
Create Index [IX_3] on [446_13042026_gtbV2] (ConsumerID,ConsumerAccountID)
go
Alter Table [446_13042026_gtbV2] add MaxPeriod bigint
go
----Fetch Max PeriodNum
Update c Set c.MaxPeriod=d.MaxPeriodNum
from [446_13042026_gtbV2] c inner join
(Select a.ConsumerID,a.ConsumerAccountID,Max(b.PeriodNum)MaxPeriodNum
From [446_13042026_gtbV2] a inner join XDSNigeriaConsumer..ConsumerAccountHistory b
on a.ConsumerID=b.ConsumerID and a.ConsumerAccountID=b.ConsumerLinkID
and b.StatusInd='A' Group by a.ConsumerID,a.ConsumerAccountID) d on c.ConsumerID=d.ConsumerID
and c.ConsumerAccountID=d.ConsumerAccountID
go
Create Index [IX_1] on [446_13042026_gtbV2] (ConsumerID,ConsumerAccountID, MaxPeriod)
go
Update a Set a.AccountStatusCode=b.AccountStatusCode,
a.CurrentBalanceAmt=b.CurrentBalanceAmt,
a.MonthsInArrears=b.MonthsInArrears,
a.AmountOverdue=b.AmountOverdue,
a.LoanClassification=b.LoanClassification,
a.ChangedByUser='446_13042026_gtb',a.ChangedOnDate=GETDATE()
from XDSNigeriaConsumer..ConsumerAccount a inner join [446_13042026_gtbV2] b on
a.ConsumerID=b.ConsumerID and a.ConsumerAccountID=b.ConsumerAccountID
go
Update a Set a.AccountStatusCode=b.AccountStatusCode,
a.CurrentBalanceAmt=b.CurrentBalanceAmt,
a.MonthsInArrears=b.MonthsInArrears,
a.AmountOverdue=b.AmountOverdue,
a.LoanClassification=b.LoanClassification,
a.ChangedByUser='446_13042026_gtb',a.ChangedOnDate=GETDATE()
from XDSNigeriaConsumer..ConsumerAccountHistory a inner join [446_13042026_gtbV2] b on
a.ConsumerID=b.ConsumerID and a.ConsumerLinkID=b.ConsumerAccountID
and a.PeriodNum=b.MaxPeriod and a.StatusInd='A'
go
----Commercial Script
go
Select * into [446_13042026_gtbV1_comm] from [446_13042026_gtbV1]
go
Create Index [IX_1] on [446_13042026_gtbV1_comm] (AccountNo)
go
----Matching
----Please change SubScriberID
Select a.*,b.AccountStatusCode as DBAccountStatusCode,b.AccountStatusDate as DBAccountStatusDate,
b.LoanClassification as DBLoanClassification,b.CommercialID,b.CommercialAccountID
into [446_13042026_gtbV2_comm]
From [446_13042026_gtbV1_comm] a inner Join
XDSNigeriaCommercial..CommercialAccount b (NoLock) on
SUBSTRING (Ltrim(Rtrim(a.AccountNo)), PATINDEX('%[^0 ]%', Ltrim(Rtrim(a.AccountNo)) + ' '), LEN(Ltrim(Rtrim(a.AccountNo))))=
SUBSTRING (Ltrim(Rtrim(b.AccountNo)), PATINDEX('%[^0 ]%', Ltrim(Rtrim(b.AccountNo)) + ' '), LEN(Ltrim(Rtrim(b.AccountNo)))) collate Latin1_General_CI_AS
and b.SubscriberID=446 and b.StatusInd='A'
-- If the Mathcing % is too low please check the subscriberid is changed or not
go
Create Index [IX_3] on [446_13042026_gtbV2_comm] (CommercialID,CommercialAccountID)
go
Alter Table [446_13042026_gtbV2_comm] add MaxPeriod bigint
go
----Fetch Max PeriodNum
Update c Set c.MaxPeriod=d.MaxPeriodNum
from [446_13042026_gtbV2_comm] c inner join
(Select a.CommercialID,a.CommercialAccountID,Max(b.PeriodNum)MaxPeriodNum
From [446_13042026_gtbV2_comm] a inner join XDSNigeriaCommercial..CommercialAccountHistory b
on a.CommercialID=b.CommercialID and a.CommercialAccountID=b.CommercialAccountID
and b.StatusInd='A' Group by a.CommercialID,a.CommercialAccountID) d on
c.CommercialID=d.CommercialID
and c.CommercialAccountID=d.CommercialAccountID
go
Alter Table [446_13042026_gtbV2_comm] Add StsCdeMatched tinyint, LoanClassMatch Tinyint
go
Create Index [IX_1] on [446_13042026_gtbV2_comm] (CommercialID,CommercialAccountID, MaxPeriod)
go
Update a Set a.AccountStatusCode=b.AccountStatusCode,
a.CurrentBalanceAmt=b.CurrentBalanceAmt,
a.MonthsInArrears=b.MonthsInArrears,
a.AmountOverdue=b.AmountOverdue,
a.LoanClassification=b.LoanClassification,
a.Updatedbyuser='446_13042026_gtb',a.Updatedondate=GETDATE()
from XDSNigeriaCommercial..CommercialAccount a inner join [446_13042026_gtbV2_comm] b on
a.CommercialID=b.CommercialID and a.CommercialAccountID=b.CommercialAccountID
go
Update a Set a.AccountStatusCode=b.AccountStatusCode,
a.CurrentBalanceAmt=b.CurrentBalanceAmt,
a.MonthsInArrears=b.MonthsInArrears,
a.AmountOverdue=b.AmountOverdue,
a.LoanClassification=b.LoanClassification,
a.Updatedbyuser='446_13042026_gtb',a.Updatedondate=GETDATE()
from XDSNigeriaCommercial..CommercialAccountHistory a inner join [446_13042026_gtbV2_comm] b on
a.CommercialID=b.CommercialID and a.CommercialAccountID=b.CommercialAccountID
and a.PeriodNum=b.MaxPeriod and a.StatusInd='A'