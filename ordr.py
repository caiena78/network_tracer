import os
import re
import requests
from urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)
import urllib
import json
import csv
import nmap


def macFormat(mac: str):
    mac=mac.upper()
    mac=mac.replace(".","")
    mac=mac.replace(":","")
    mac=mac.replace("-","")
    mac=mac[0:2]+":"+mac[2:4]+":"+mac[4:6]+":"+mac[6:8]+":"+mac[8:10]+":"+mac[10:12]
    return mac



def pattern_match(pattern,data,none=False):
    match=re.findall(pattern,data)
    if match:
        return match[0].strip()
    if none == True:    
        return None
    return ""

def ping(hostname):  
    # print("Pinging %s" % hostname)      
    response = os.popen("ping -n 1 %s " % hostname).read()   
    # and then check the response...
    pingpattern=r'Reply from .+: bytes=32 time.+ TTL=[0-9]{1,3}'
    if pattern_match(pingpattern,response,True) is not None:
        return True
    return False

def macAddress(baseurl:str,mac:str,login:dict)->json:
    mac = macFormat(mac)
    params=[('mac',mac),('tenantGuid','TNT-89CB1X98ASB1PXASG1O')]
    data=getresponce(baseurl,params,login['user'],login['password'])
    #print(json.dumps(data,indent=4))
    return data

def ipAddress(baseurl:str,ip:str,login:dict)->json:
    params=[('ip',ip.strip()),('tenantGuid','TNT-89CB1X98ASB1PXASG1O')]
    data=getresponce(baseurl,params,login['user'],login['password'])
    return data



def getresponce(url,parms,user,password):
    # set REST API headers
    headers = {"Accept": "application/json"}
    return requests.get(url,params=parms, headers=headers, auth=(user, password), verify=False).json()
    #return requests.get(url, headers=headers, auth=(user, password), verify=False).json()


def getLoginObject(user:str,password:str)->dict:
    return {
    "user":user,
    "password":password
    }
 



def write_list_of_dicts_to_csv(data_list, filename):
    """
    Writes a list of dictionaries to a CSV file.
    Each dictionary represents a row, and keys are used as column headers.
    """
    if not data_list:
        raise ValueError("The data list is empty.")

    headers = data_list[0].keys()

    with open(filename, mode='w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=headers)
        writer.writeheader()
        writer.writerows(data_list)



def detect_os(ip_address):
    """
    Uses Nmap to scan the given IP address and attempt OS detection.
    Returns the OS details if available.
    """
    scanner = nmap.PortScanner()
    try:
        scanner.scan(ip_address, arguments='-O')  # -O enables OS detection
        if 'osmatch' in scanner[ip_address]:
            os_matches = scanner[ip_address]['osmatch']
            if os_matches:
                return os_matches[0]['name']  # Most likely OS match
        return "OS detection failed or not available."
    except Exception as e:
        return f"Scan failed: {e}"


def convert_dicts_to_csv_lines(dict_list):
    """
    Converts a list of dictionaries into a list of comma-separated strings.
    Each dictionary is converted to a string with its values separated by commas.
    """
    csv_lines = []
    for item in dict_list:
        line = ','.join(str(item.get(key, "")) for key in item.keys())
        csv_lines.append(line)
    return csv_lines




ordr_info=[]
login=getLoginObject(<ORDR_USERNAME>,ORDR_PASSWORD)
url="<ORDR_URL"
params=[('mac','1C:D1:E0:D2:D8:14'),('tenantGuid','TNT-89CB1X98ASB1PXASG1O')]
with open("client.txt",'r') as r:
    data=r.readlines()
for ip in data:
    ordr_data=ipAddress(url,ip,login)
    if "OsType" not in ordr_data:
        data=detect_os(ip.strip())
        if "failed" not in data:
            ordr_data['OsType']=data
        print(data)
    ordr_info.append(ordr_data)

csvdata=convert_dicts_to_csv_lines(ordr_info)
with open("data.csv","w") as w:
    for c in csvdata:
        w.write(c+"\n")

# with open("ordr.json","w") as w:
#     w.write(json.dumps(ordr_info,indent=4))


    
