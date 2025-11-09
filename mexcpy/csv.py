import pandas as pd
import glob
import os

# --- PARAMETERS ---

# 1. This is the path to the folder where you unzipped ALL the daily CSV files.
#    Use 'r' before the string to make sure paths with \ work correctly.
#    EXAMPLE: folder_path = r"C:\Users\Filipe\Desktop\sui_daily_csvs"
folder_path = r"C:\path\to\your\sui_daily_csvs"

# 2. The name of your final combined file
output_filename = "SUIUSDT_5m_history_COMBINED.csv"

# --- SCRIPT ---

print(f"Looking for CSV files in: {folder_path}\n")

# Use glob to find all files in the folder that end with .csv
# The '*' is a wildcard that matches anything.
all_files = glob.glob(os.path.join(folder_path, "*.csv"))

if not all_files:
    print(f"Error: No CSV files found in that folder.")
    print("Please check the 'folder_path' variable and make sure your CSVs are unzipped there.")
else:
    print(f"Found {len(all_files)} CSV files. Combining them...")

    # Create a list to hold all the individual DataFrames
    li = []

    for filename in all_files:
        try:
            # Read each CSV file into a DataFrame
            df = pd.read_csv(filename)
            li.append(df)
        except Exception as e:
            print(f"  Warning: Could not read {filename}. Error: {e}")

    # Combine all DataFrames in the list into one single DataFrame
    frame = pd.concat(li, axis=0, ignore_index=True)

    print(f"\nCombined {len(li)} files into one DataFrame with {len(frame)} rows.")

    # --- Data Cleaning and Sorting ---
    # The CSVs from MEXC often use 'open_time' as the header for the timestamp
    # If your column is named 'OpenTime' or something else, just change it here.
    time_column = 'open_time'

    if time_column not in frame.columns:
        print(f"Error: Could not find timestamp column named '{time_column}'.")
        print(f"Found columns: {frame.columns.to_list()}")
    else:
        # Convert the timestamp to a readable datetime object
        # The downloads use milliseconds (unit='ms')
        frame[time_column] = pd.to_datetime(frame[time_column], unit='ms')

        # Sort the entire DataFrame by the open time to ensure it's in order
        frame.sort_values(by=time_column, inplace=True)

        # Drop any duplicates (e.g., from file overlaps)
        frame.drop_duplicates(subset=[time_column], inplace=True)

        # Save the final, combined file
        frame.to_csv(output_filename, index=False)

        print(f"\n✅ Success! Saved {len(frame)} unique candles to {output_filename}")
        if not frame.empty:
            print(f"  Earliest candle: {frame.iloc[0][time_column]}")
            print(f"  Latest candle:   {frame.iloc[-1][time_column]}")