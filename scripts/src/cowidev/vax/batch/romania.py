import pandas as pd

from cowidev.utils import paths
from cowidev.utils.utils import check_known_columns
from cowidev.utils.web import request_json
from cowidev.vax.utils.files import export_metadata_manufacturer
from cowidev.vax.utils.utils import make_monotonic


class Romania:
    source_url: str = "https://d35p9e4fm9h3wo.cloudfront.net/latestData.json"
    source_url_ref: str = "https://datelazi.ro/"
    location: str = "Romania"
    columns_rename: dict = {
        "index": "date",
        "numberTotalDosesAdministered": "total_vaccinations",
    }
    vaccine_mapping: dict = {
        "pfizer": "Pfizer/BioNTech",
        "moderna": "Moderna",
        "astra_zeneca": "Oxford/AstraZeneca",
        "johnson_and_johnson": "Johnson&Johnson",
    }
    vaccines_1d: list = ["johnson_and_johnson"]

    def read(self) -> pd.DataFrame:
        data = request_json(self.source_url)
        df = pd.DataFrame.from_dict(data["historicalData"], orient="index")
        check_known_columns(
            df,
            [
                "parsedOn",
                "parsedOnString",
                "fileName",
                "complete",
                "averageAge",
                "numberInfected",
                "numberCured",
                "numberDeceased",
                "percentageOfWomen",
                "percentageOfMen",
                "percentageOfChildren",
                "numberTotalDosesAdministered",
                "distributionByAge",
                "countyInfectionsNumbers",
                "incidence",
                "large_cities_incidence",
                "small_cities_incidence",
                "vaccines",
            ],
        )
        return df[["vaccines", "numberTotalDosesAdministered"]].reset_index().dropna().sort_values(by="index")

    def pipe_rename_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.rename(columns=self.columns_rename)

    def pipe_location(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.assign(location=self.location)

    def _unnest_vaccine_details(self, df: pd.DataFrame) -> pd.DataFrame:
        def _doses_by_vax(x):
            return {k: v["total_administered"] for k, v in x.items()}

        df_vax = pd.DataFrame.from_records(df.vaccines.apply(_doses_by_vax), index=df.index)
        # Check vaccine names - Any new ones?
        vaccines_unknown = set(df_vax.columns).difference(self.vaccine_mapping)
        if vaccines_unknown:
            raise ValueError(f"Unrecognized vaccine {vaccines_unknown}")
        df_vax.columns = [self.vaccine_mapping[col] for col in df_vax.columns]
        return df_vax.merge(df, left_index=True, right_index=True)

    def pipeline_base(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.pipe(self.pipe_rename_columns).pipe(self.pipe_location).pipe(self._unnest_vaccine_details)

    def pipe_source(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.assign(source_url=self.source_url_ref)

    def pipe_people_fully_vaccinated(self, df: pd.DataFrame) -> pd.DataFrame:
        def _people_fully_vaccinated(x):
            return sum(v["immunized"] for v in x.values())

        return df.assign(people_fully_vaccinated=df.vaccines.apply(_people_fully_vaccinated).cumsum())

    def pipe_people_vaccinated(self, df: pd.DataFrame) -> pd.DataFrame:
        def _people_fully_vaccinated_1d(x):
            return sum(v["immunized"] for k, v in x.items() if k in self.vaccines_1d)

        people_fully_vaccinated_1d = df.vaccines.apply(_people_fully_vaccinated_1d).cumsum()
        df = df.assign(
            people_vaccinated=(df.total_vaccinations - df.people_fully_vaccinated + people_fully_vaccinated_1d)
        )

        # Booster doses have started on September 28, 2021. Because of this, the calculation
        # performed here no longer holds, and people_vaccinated is left blank for now.
        df.loc[df.date >= "2021-09-28", "people_vaccinated"] = pd.NA
        return df

    def pipe_add_latest_who(self, df: pd.DataFrame) -> pd.DataFrame:
        who = pd.read_csv(
            "https://covid19.who.int/who-data/vaccination-data.csv",
            usecols=["COUNTRY", "DATA_SOURCE", "DATE_UPDATED", "PERSONS_VACCINATED_1PLUS_DOSE"],
        )

        who = who[(who.COUNTRY == "Romania") & (who.DATA_SOURCE == "REPORTING")]
        if len(who) == 0:
            return df

        last_who_report_date = who.DATE_UPDATED.values[0]
        df.loc[df.date == last_who_report_date, "total_vaccinations"] = pd.NA
        df.loc[df.date == last_who_report_date, "people_vaccinated"] = who.PERSONS_VACCINATED_1PLUS_DOSE.values[0]
        df.loc[df.date == last_who_report_date, "people_fully_vaccinated"] = pd.NA
        df.loc[df.date == last_who_report_date, "source_url"] = "https://covid19.who.int/"
        return df

    def _vaccine_start_dates(self, df: pd.DataFrame):
        date2vax = sorted(
            ((df.loc[df[vaccine] > 0, "date"].min(), vaccine) for vaccine in self.vaccine_mapping.values()),
            key=lambda x: x[0],
            reverse=True,
        )
        return [(date2vax[i][0], ", ".join(sorted(v[1] for v in date2vax[i:]))) for i in range(len(date2vax))]

    def pipe_vaccine(self, df: pd.DataFrame) -> pd.DataFrame:
        vax_date_mapping = self._vaccine_start_dates(df)

        def _enrich_vaccine(date: str) -> str:
            for dt, vaccines in vax_date_mapping:
                if date >= dt:
                    return vaccines
            raise ValueError(f"Invalid date {date} in DataFrame!")

        return df.assign(vaccine=df.date.apply(_enrich_vaccine))

    def pipe_select_output_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        return df[
            [
                "date",
                "location",
                "vaccine",
                "source_url",
                "total_vaccinations",
                "people_vaccinated",
                "people_fully_vaccinated",
            ]
        ]

    def pipeline(self, df: pd.DataFrame) -> pd.DataFrame:
        return (
            df.pipe(self.pipe_source)
            .pipe(self.pipe_people_fully_vaccinated)
            .pipe(self.pipe_people_vaccinated)
            .pipe(self.pipe_vaccine)
            .pipe(self.pipe_select_output_columns)
            .pipe(self.pipe_add_latest_who)
            .pipe(make_monotonic)
        )

    def pipe_manufacturer_melt(self, df: pd.DataFrame) -> pd.DataFrame:
        id_vars = ["date", "location"]
        df = df[id_vars + list(self.vaccine_mapping.values())].melt(
            id_vars=id_vars, var_name="vaccine", value_name="total_vaccinations"
        )
        return df[df.total_vaccinations != 0]

    def pipe_manufacturer_cumsum(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.assign(total_vaccinations=df.groupby("vaccine", as_index=False).cumsum())

    def pipeline_manufacturer(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.pipe(self.pipe_manufacturer_melt).pipe(self.pipe_manufacturer_cumsum)

    def export(self):
        df_base = self.read().pipe(self.pipeline_base)
        # Export data
        df = df_base.copy().pipe(self.pipeline)
        df.to_csv(paths.out_vax(self.location), index=False)
        # Export manufacturer data
        df = df_base.copy().pipe(self.pipeline_manufacturer)
        df.to_csv(paths.out_vax(self.location, manufacturer=True), index=False)
        export_metadata_manufacturer(
            df,
            "Government of Romania via datelazi.ro",
            self.source_url,
        )


def main():
    Romania().export()


if __name__ == "__main__":
    main()
